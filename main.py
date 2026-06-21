"""
yt-dlp GUI backend
-------------------
A FastAPI wrapper around yt-dlp that turns your cheat sheet commands into
clickable buttons + API endpoints.

This version adds:
  - Postgres persistence (via Neon) so job history survives restarts
  - Cloudflare R2 storage so finished files survive restarts/redeploys
  - A shared-passphrase login gate so only people you've shared the
    passphrase with can use the app
"""

import os
import uuid
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict

import yt_dlp
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import init_db, get_db, JobRecord
from storage import upload_file, delete_local_file, get_presigned_download_url, delete_from_bucket
from auth import (
    check_passphrase,
    create_session_token,
    require_login,
    is_logged_in,
    COOKIE_NAME,
    COOKIE_MAX_AGE,
)

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).parent
DOWNLOADS_DIR = APP_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="yt-dlp GUI", description="Local control panel for yt-dlp")

# Run-time job state lives here (fast, lock-protected, no DB round-trip per
# progress tick). Each Job is synced to Postgres at the meaningful
# checkpoints: creation, finished/error/cancelled, and final file-ready.
# This avoids hammering the database tens of times a second while a
# progress bar updates, while still giving you durable history.
JOBS: Dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    UPLOADING = "uploading"   # new: finished downloading, now pushing to R2
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class Mode(str, Enum):
    MP3 = "mp3"
    MP4 = "mp4"
    PLAYLIST = "playlist"
    PLAYLIST_RANGE = "playlist_range"
    CUSTOM = "custom"
    FACEBOOK = "facebook"
    X = "x"
    ZIP_TASK = "zip_task"


class Job:
    def __init__(self, job_id: str, url: str, mode: Mode, options: dict):
        self.id = job_id
        self.url = url
        self.mode = mode
        self.options = options
        self.status = JobStatus.QUEUED
        self.progress: float = 0.0          # 0-100
        self.speed: Optional[str] = None
        self.eta: Optional[str] = None
        self.filename: Optional[str] = None
        self.size_downloaded: Optional[str] = None
        self.size_total: Optional[str] = None
        self.log: List[str] = []
        self.error: Optional[str] = None
        self.created_at = datetime.utcnow()
        self.cancel_requested = False
        self.storage_key: Optional[str] = None
        self.download_ready = False
        self._logger: Optional["JobLogger"] = None

    def add_log(self, line: str):
        self.log.append(line)
        if len(self.log) > 300:
            self.log = self.log[-300:]

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "options": self.options,
            "status": self.status,
            "progress": round(self.progress, 1),
            "speed": self.speed,
            "eta": self.eta,
            "filename": self.filename,
            "size_downloaded": self.size_downloaded,
            "size_total": self.size_total,
            "log_tail": self.log[-15:],
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "download_ready": self.download_ready,
        }

    def persist(self, db: Session):
        """Upsert this job's current state into Postgres."""
        record = db.get(JobRecord, self.id)
        if record is None:
            record = JobRecord(id=self.id)
            db.add(record)

        record.url = self.url
        record.mode = self.mode.value if isinstance(self.mode, Mode) else self.mode
        record.options = self.options
        record.status = self.status.value if isinstance(self.status, JobStatus) else self.status
        record.progress = self.progress
        record.speed = self.speed
        record.eta = self.eta
        record.filename = self.filename
        record.size_downloaded = self.size_downloaded
        record.size_total = self.size_total
        record.storage_key = self.storage_key
        record.download_ready = self.download_ready
        record.log = self.log
        record.error = self.error
        record.created_at = self.created_at
        record.cancel_requested = self.cancel_requested
        db.commit()

    @classmethod
    def from_record(cls, record: JobRecord) -> "Job":
        """Rehydrate a Job (for API responses) from a DB row."""
        job = cls(record.id, record.url, record.mode, record.options or {})
        job.status = record.status
        job.progress = record.progress or 0.0
        job.speed = record.speed
        job.eta = record.eta
        job.filename = record.filename
        job.size_downloaded = record.size_downloaded
        job.size_total = record.size_total
        job.storage_key = record.storage_key
        job.download_ready = record.download_ready
        job.log = record.log or []
        job.error = record.error
        job.created_at = record.created_at
        job.cancel_requested = record.cancel_requested
        return job


def _persist(job: "Job"):
    """Helper to persist a job from inside the background thread (own DB session)."""
    db = next(get_db())
    try:
        job.persist(db)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Request schema
# --------------------------------------------------------------------------

class DownloadRequest(BaseModel):
    url: str = Field(..., description="Video/playlist URL")
    mode: Mode = Mode.MP4
    resolution: Optional[int] = None
    embed_thumbnail: bool = True
    add_metadata: bool = True
    output_template: Optional[str] = None
    subfolder: Optional[str] = None
    playlist_start: Optional[int] = None
    playlist_end: Optional[int] = None
    search_first: bool = False
    search_count: int = 5
    filter_keywords: Optional[str] = "lyrics|visualizer|audio|bass.boosted|karaoke"
    user_agent: Optional[str] = None
    cookies_file: Optional[str] = None
    use_archive: bool = True
    raw_args: Optional[str] = None


class BatchRequest(BaseModel):
    urls: str = Field(..., description="One URL per line.")
    mode: Mode = Mode.MP4
    resolution: Optional[int] = None
    embed_thumbnail: bool = True
    add_metadata: bool = True
    subfolder: Optional[str] = None
    use_archive: bool = True
    cookies_file: Optional[str] = None
    user_agent: Optional[str] = None
    zip_when_done: bool = False
    zip_password: Optional[str] = None


class LoginRequest(BaseModel):
    passphrase: str


# --------------------------------------------------------------------------
# yt-dlp options builder
# --------------------------------------------------------------------------

def build_ydl_opts(job: Job, req: DownloadRequest) -> dict:
    out_dir = DOWNLOADS_DIR / (req.subfolder if req.subfolder else "")
    out_dir.mkdir(parents=True, exist_ok=True)

    template = req.output_template or "%(title)s.%(ext)s"
    if req.mode in (Mode.PLAYLIST, Mode.PLAYLIST_RANGE) and not req.output_template:
        template = "%(playlist_index)s - %(title)s.%(ext)s"

    job_logger = JobLogger(job)
    job._logger = job_logger

    opts: dict = {
        "outtmpl": str(out_dir / template),
        "noplaylist": req.mode not in (Mode.PLAYLIST, Mode.PLAYLIST_RANGE),
        "progress_hooks": [lambda d: _progress_hook(job, d)],
        "logger": job_logger,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "trim_file_name": 150,

        # NETWORK RESILIENCE:
        # float("inf") is the correct Python-API value for "retry forever".
        # The string "infinite" only works as a CLI flag (argparse converts
        # it before it reaches yt-dlp's internals) — passed directly through
        # the Python API it causes a crash comparing int to str.
        "retries": float("inf"),
        "fragment_retries": float("inf"),
        "file_access_retries": float("inf"),
    }

    if req.use_archive:
        opts["download_archive"] = str(out_dir / "archive.txt")

    if req.mode == Mode.MP3:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"

    postprocessors = opts.get("postprocessors", [])
    if req.embed_thumbnail:
        opts["writethumbnail"] = True
        postprocessors.append({"key": "EmbedThumbnail"})
    if req.add_metadata:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
    opts["postprocessors"] = postprocessors

    if req.mode == Mode.PLAYLIST_RANGE:
        if req.playlist_start:
            opts["playliststart"] = req.playlist_start
        if req.playlist_end:
            opts["playlistend"] = req.playlist_end

    if req.search_first:
        opts["default_search"] = f"ytsearch{req.search_count}"
        if req.filter_keywords:
            opts["match_filter"] = yt_dlp.utils.match_filter_func(f"title !~= (?i)({req.filter_keywords})")
        opts["max_downloads"] = 1

    if req.mode == Mode.FACEBOOK or req.user_agent:
        opts["http_headers"] = {
            "User-Agent": req.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

    if req.mode == Mode.X and not req.output_template:
        opts["outtmpl"] = str(out_dir / "%(uploader_id)s_%(id)s.%(ext)s")

    if req.cookies_file:
        cookie_path = Path(req.cookies_file)
        if cookie_path.exists():
            opts["cookiefile"] = str(cookie_path)
        else:
            job.add_log(f"Warning: cookies file '{req.cookies_file}' not found, continuing without it")

    if req.raw_args:
        opts.setdefault("_raw_args_note", req.raw_args)
        job.add_log(f"Note: raw_args parsed: {req.raw_args}")

    return opts


class JobLogger:
    def __init__(self, job: Job):
        self.job = job
        self.had_error = False
        self.last_error = None

    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        self.job.add_log(msg)

    def info(self, msg):
        self.job.add_log(msg)

    def warning(self, msg):
        if "Retrying" in msg or "Giving up after" in msg:
            self.job.add_log(f"Network issue/interruption: {msg} (Will keep retrying)")
        else:
            self.job.add_log(f"Warning: {msg}")

    def error(self, msg):
        if "max-downloads" in msg or "Maximum number of downloads reached" in msg:
            self.job.add_log(f"Stopped after first match (by design): {msg}")
            return
        self.had_error = True
        self.last_error = msg
        self.job.add_log(f"Error: {msg}")


def _progress_hook(job: Job, d: dict):
    if job.cancel_requested:
        raise yt_dlp.utils.DownloadError("Cancelled by user")

    if d["status"] == "downloading":
        job.status = JobStatus.RUNNING
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)

        job.size_downloaded = d.get("_downloaded_bytes_str", "").strip() or None
        job.size_total = d.get("_total_bytes_str", "").strip() or d.get("_total_bytes_estimate_str", "").strip() or None

        if total:
            job.progress = downloaded / total * 100
        job.speed = d.get("_speed_str", "").strip() or None
        job.eta = d.get("_eta_str", "").strip() or None
        fname = d.get("filename")
        if fname:
            job.filename = os.path.basename(fname)

    elif d["status"] == "finished":
        job.progress = 100.0
        job.add_log("Finished downloading, now processing (thumbnail/metadata)...")


# --------------------------------------------------------------------------
# Workers (Download, Upload-to-R2, & Zip)
# --------------------------------------------------------------------------

def run_job(job: Job, req: DownloadRequest):
    job.status = JobStatus.RUNNING
    job.add_log(f"Starting [{job.mode}] for: {job.url}")
    _persist(job)

    try:
        opts = build_ydl_opts(job, req)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([job.url])

        if job.cancel_requested:
            job.status = JobStatus.CANCELLED
            job.add_log("Cancelled.")
            _persist(job)
            return

        if job._logger and job._logger.had_error:
            job.status = JobStatus.ERROR
            job.error = job._logger.last_error
            job.add_log("Finished with errors — see log above.")
            _persist(job)
            return

        # Download succeeded — now push the finished file to R2 so it
        # survives restarts/redeploys instead of sitting on ephemeral disk.
        if job.filename:
            out_dir = DOWNLOADS_DIR / (req.subfolder if req.subfolder else "")
            local_path = out_dir / job.filename
            if local_path.exists():
                job.status = JobStatus.UPLOADING
                job.add_log("Uploading finished file to cloud storage...")
                _persist(job)

                try:
                    storage_key = upload_file(str(local_path), job.id, job.filename)
                    job.storage_key = storage_key
                    job.download_ready = True
                    job.add_log("Upload complete. File ready to download.")
                    delete_local_file(str(local_path))
                except Exception as upload_err:
                    job.add_log(f"Warning: cloud upload failed ({upload_err}). "
                                f"File remains on local disk only (may not survive a restart).")
            else:
                job.add_log(f"Warning: expected file '{job.filename}' not found on disk after download.")

        job.status = JobStatus.DONE
        job.add_log("Done.")
        _persist(job)

    except yt_dlp.utils.DownloadError as e:
        if job.cancel_requested:
            job.status = JobStatus.CANCELLED
            job.add_log("Cancelled.")
        else:
            job.status = JobStatus.ERROR
            job.error = str(e)
            job.add_log(f"Error: {e}")
        _persist(job)
    except Exception as e:
        job.status = JobStatus.ERROR
        job.error = str(e)
        job.add_log(f"Unexpected error: {e}")
        _persist(job)


def watch_batch_and_zip(target_job_ids: List[str], zip_job_id: str, subfolder: str, password: Optional[str]):
    """Background thread that monitors a batch of downloads and zips them when all complete."""
    while True:
        with JOBS_LOCK:
            statuses = [JOBS[jid].status for jid in target_job_ids if jid in JOBS]
        if all(s in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED) for s in statuses):
            break
        time.sleep(2)

    with JOBS_LOCK:
        zjob = JOBS.get(zip_job_id)
    if not zjob or zjob.cancel_requested:
        return

    if all(s in (JobStatus.ERROR, JobStatus.CANCELLED) for s in statuses):
        zjob.status = JobStatus.CANCELLED
        zjob.add_log("All downloads in batch failed or were cancelled. Skipping zip.")
        _persist(zjob)
        return

    zjob.status = JobStatus.RUNNING
    _persist(zjob)
    folder_path = DOWNLOADS_DIR / subfolder
    zip_path = DOWNLOADS_DIR / f"{subfolder}.zip"
    zjob.add_log(f"Zipping folder '{subfolder}' to '{zip_path.name}'...")

    if not folder_path.exists() or not any(folder_path.iterdir()):
        zjob.status = JobStatus.ERROR
        zjob.error = "Folder is empty or missing"
        _persist(zjob)
        return

    try:
        import pyzipper

        files_to_zip = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                files_to_zip.append(os.path.join(root, f))

        kwargs = {"compression": pyzipper.ZIP_DEFLATED}
        if password:
            kwargs["encryption"] = pyzipper.WZ_AES

        with pyzipper.AESZipFile(zip_path, "w", **kwargs) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))

            for i, file_path in enumerate(files_to_zip):
                arcname = os.path.relpath(file_path, folder_path)
                zf.write(file_path, arcname)
                zjob.progress = ((i + 1) / len(files_to_zip)) * 100

        zjob.status = JobStatus.UPLOADING
        zjob.add_log("Uploading zip to cloud storage...")
        _persist(zjob)

        try:
            storage_key = upload_file(str(zip_path), zjob.id, zip_path.name)
            zjob.storage_key = storage_key
            zjob.download_ready = True
            zjob.filename = zip_path.name
            delete_local_file(str(zip_path))
        except Exception as upload_err:
            zjob.add_log(f"Warning: cloud upload failed ({upload_err}). Zip remains on local disk only.")

        zjob.status = JobStatus.DONE
        zjob.add_log(f"Done! Created encrypted zip: {zip_path.name}")
        _persist(zjob)
    except Exception as e:
        zjob.status = JobStatus.ERROR
        zjob.error = f"Zipping failed: {e}"
        zjob.add_log(f"Error zipping: {e}")
        _persist(zjob)


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    # Rehydrate in-memory JOBS from the database so /api/jobs has history
    # immediately after a restart, instead of starting empty.
    db = next(get_db())
    try:
        records = db.query(JobRecord).all()
        with JOBS_LOCK:
            for record in records:
                JOBS[record.id] = Job.from_record(record)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.post("/api/login")
def login(req: LoginRequest, response: Response):
    if not check_passphrase(req.passphrase):
        raise HTTPException(status_code=401, detail="Incorrect passphrase")

    token = create_session_token()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,  # cookie only sent over HTTPS — Render serves you HTTPS by default
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"logged_in": is_logged_in(request)}


# --------------------------------------------------------------------------
# API routes (all protected by require_login)
# --------------------------------------------------------------------------

@app.post("/api/jobs", dependencies=[Depends(require_login)])
def create_job(req: DownloadRequest):
    job_id = uuid.uuid4().hex[:10]
    job = Job(job_id, req.url, req.mode, req.dict())

    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist(job)

    thread = threading.Thread(target=run_job, args=(job, req), daemon=True)
    thread.start()

    return job.to_dict()


@app.post("/api/jobs/batch", dependencies=[Depends(require_login)])
def create_batch(batch: BatchRequest):
    lines = [
        line.strip()
        for line in batch.urls.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise HTTPException(400, "No URLs found in the list")

    subfolder = batch.subfolder
    if batch.zip_when_done and not subfolder:
        subfolder = f"Batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    created = []
    job_ids = []

    for url in lines:
        req = DownloadRequest(
            url=url,
            mode=batch.mode,
            resolution=batch.resolution,
            embed_thumbnail=batch.embed_thumbnail,
            add_metadata=batch.add_metadata,
            subfolder=subfolder,
            use_archive=batch.use_archive,
            cookies_file=batch.cookies_file,
            user_agent=batch.user_agent,
        )
        j = create_job(req)
        created.append(j)
        job_ids.append(j["id"])

    if batch.zip_when_done:
        zip_job_id = uuid.uuid4().hex[:10]
        zip_job = Job(zip_job_id, f"Zip Process: {subfolder}.zip", Mode.ZIP_TASK, {})
        with JOBS_LOCK:
            JOBS[zip_job_id] = zip_job
        _persist(zip_job)

        threading.Thread(
            target=watch_batch_and_zip,
            args=(job_ids, zip_job_id, subfolder, batch.zip_password),
            daemon=True,
        ).start()
        created.append(zip_job.to_dict())

    return {"count": len(created), "jobs": created}


@app.get("/api/jobs", dependencies=[Depends(require_login)])
def list_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs]


@app.delete("/api/jobs/clear-finished", dependencies=[Depends(require_login)])
def clear_finished(db: Session = Depends(get_db)):
    removed = 0
    with JOBS_LOCK:
        finished_ids = [
            jid for jid, j in JOBS.items()
            if j.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)
        ]
        for jid in finished_ids:
            job = JOBS[jid]
            if job.storage_key:
                delete_from_bucket(job.storage_key)
            del JOBS[jid]
            removed += 1

    for jid in finished_ids:
        record = db.get(JobRecord, jid)
        if record:
            db.delete(record)
    db.commit()

    return {"removed": removed}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_login)])
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_login)])
def get_download_url(job_id: str):
    """Returns a pre-signed R2 URL (valid ~1hr) for the finished file."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.download_ready or not job.storage_key:
        raise HTTPException(409, "File isn't ready yet")

    url = get_presigned_download_url(job.storage_key, expires_in=3600)
    return {"url": url, "expires_in": 3600}


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_login)])
def delete_job(job_id: str, db: Session = Depends(get_db)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job.cancel_requested = True
        _persist(job)
        return {"status": "cancel_requested"}
    else:
        if job.storage_key:
            delete_from_bucket(job.storage_key)
        with JOBS_LOCK:
            del JOBS[job_id]
        record = db.get(JobRecord, job_id)
        if record:
            db.delete(record)
            db.commit()
        return {"status": "removed"}


@app.put("/api/jobs/{job_id}/retry", dependencies=[Depends(require_login)])
def retry_job(job_id: str):
    old = JOBS.get(job_id)
    if not old:
        raise HTTPException(404, "Job not found")
    req = DownloadRequest(**old.options)
    return create_job(req)


@app.get("/api/health")
def health():
    return {"ok": True, "yt_dlp_version": yt_dlp.version.__version__}


# --------------------------------------------------------------------------
# Frontend (static files + login gate)
# --------------------------------------------------------------------------
# Static assets (CSS/JS/images) are served unauthenticated, since they're
# not sensitive — but index.html and any other page is gated by checking
# the session cookie before falling through to the static file handler.
# /login itself is always served so people can actually reach the login form.

@app.get("/login")
def login_page():
    login_path = APP_DIR / "static" / "login.html"
    if login_path.exists():
        return FileResponse(str(login_path))
    raise HTTPException(404, "login.html not found in app/static/")


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # Always allow: the login page itself, the login/logout API calls,
    # health check, and static assets needed to render the login page.
    public_paths = ("/login", "/api/login", "/api/logout", "/api/auth/status", "/api/health")
    is_static_asset = path.startswith("/static/") or path in ("/favicon.ico",)

    if path in public_paths or is_static_asset:
        return await call_next(request)

    if not is_logged_in(request):
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Not logged in"})
        return RedirectResponse(url="/login")

    return await call_next(request)


app.mount("/", StaticFiles(directory=str(APP_DIR / "static"), html=True), name="static")
