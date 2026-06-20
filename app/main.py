"""
yt-dlp GUI backend
-------------------
A FastAPI wrapper around yt-dlp that turns your cheat sheet commands into
clickable buttons + API endpoints. 
"""

import os
import re
import uuid
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Union

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, validator

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).parent
DOWNLOADS_DIR = APP_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="yt-dlp GUI", description="Local control panel for yt-dlp")

JOBS: Dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
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
        self.created_at = datetime.now().isoformat()
        self.cancel_requested = False
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
            "created_at": self.created_at,
        }


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
    
    # New: Batch zipping options
    zip_when_done: bool = False
    zip_password: Optional[str] = None


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
        # Infinite retries allows it to gracefully pause if internet drops 
        # and immediately resume when the connection returns.
        # NOTE: "infinite" is a CLI-only shorthand that argparse converts to
        # float('inf') before it reaches yt-dlp's internals. The Python API
        # (YoutubeDL(opts)) skips that conversion, so passing the string
        # "infinite" here causes `retry_count <= retries` to compare an int
        # to a str and crash. float('inf') is the correct value to pass directly.
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
        # Temporarily disable resolution limiting to fix basic downloads
        # TODO: Fix yt-dlp type comparison error with height-based format selector
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
        if req.playlist_start: opts["playliststart"] = req.playlist_start
        if req.playlist_end: opts["playlistend"] = req.playlist_end

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
        if msg.startswith("[debug] "): return
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
        
        # Extract formatted size strings provided natively by yt-dlp
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
        job.add_log(f"Finished downloading, now processing (thumbnail/metadata)...")


# --------------------------------------------------------------------------
# Workers (Download & Zip)
# --------------------------------------------------------------------------

def run_job(job: Job, req: DownloadRequest):
    job.status = JobStatus.RUNNING
    job.add_log(f"Starting [{job.mode}] for: {job.url}")
    try:
        opts = build_ydl_opts(job, req)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([job.url])

        if job.cancel_requested:
            job.status = JobStatus.CANCELLED
            job.add_log("Cancelled.")
        elif job._logger and job._logger.had_error:
            job.status = JobStatus.ERROR
            job.error = job._logger.last_error
            job.add_log("Finished with errors — see log above.")
        else:
            job.status = JobStatus.DONE
            job.add_log("Done.")
    except yt_dlp.utils.DownloadError as e:
        if job.cancel_requested:
            job.status = JobStatus.CANCELLED
            job.add_log("Cancelled.")
        else:
            job.status = JobStatus.ERROR
            job.error = str(e)
            job.add_log(f"Error: {e}")
    except Exception as e:
        job.status = JobStatus.ERROR
        job.error = str(e)
        job.add_log(f"Unexpected error: {e}")


def watch_batch_and_zip(target_job_ids: List[str], zip_job_id: str, subfolder: str, password: Optional[str]):
    """Background thread that monitors a batch of downloads and zips them when all complete."""
    while True:
        with JOBS_LOCK:
            statuses = [JOBS[jid].status for jid in target_job_ids if jid in JOBS]
        # Wait until no jobs are still queued or running
        if all(s in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED) for s in statuses):
            break
        time.sleep(2)
        
    with JOBS_LOCK:
        zjob = JOBS.get(zip_job_id)
    if not zjob or zjob.cancel_requested:
        return

    # If all batch items failed, skip zipping
    if all(s in (JobStatus.ERROR, JobStatus.CANCELLED) for s in statuses):
        zjob.status = JobStatus.CANCELLED
        zjob.add_log("All downloads in batch failed or were cancelled. Skipping zip.")
        return

    zjob.status = JobStatus.RUNNING
    folder_path = DOWNLOADS_DIR / subfolder
    zip_path = DOWNLOADS_DIR / f"{subfolder}.zip"
    zjob.add_log(f"Zipping folder '{subfolder}' to '{zip_path.name}'...")

    if not folder_path.exists() or not any(folder_path.iterdir()):
        zjob.status = JobStatus.ERROR
        zjob.error = "Folder is empty or missing"
        return

    try:
        import pyzipper
        
        files_to_zip = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                files_to_zip.append(os.path.join(root, f))
                
        kwargs = {'compression': pyzipper.ZIP_DEFLATED}
        if password:
            kwargs['encryption'] = pyzipper.WZ_AES
            
        with pyzipper.AESZipFile(zip_path, 'w', **kwargs) as zf:
            if password:
                zf.setpassword(password.encode('utf-8'))
            
            for i, file_path in enumerate(files_to_zip):
                arcname = os.path.relpath(file_path, folder_path)
                zf.write(file_path, arcname)
                zjob.progress = ((i + 1) / len(files_to_zip)) * 100
                
        zjob.status = JobStatus.DONE
        zjob.add_log(f"Done! Created encrypted zip: {zip_path.name}")
    except Exception as e:
        zjob.status = JobStatus.ERROR
        zjob.error = f"Zipping failed: {e}"
        zjob.add_log(f"Error zipping: {e}")


# --------------------------------------------------------------------------
# API routes
# --------------------------------------------------------------------------

@app.post("/api/jobs")
def create_job(req: DownloadRequest):
    job_id = uuid.uuid4().hex[:10]
    job = Job(job_id, req.url, req.mode, req.dict())

    with JOBS_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=run_job, args=(job, req), daemon=True)
    thread.start()

    return job.to_dict()


@app.post("/api/jobs/batch")
def create_batch(batch: BatchRequest):
    lines = [
        line.strip()
        for line in batch.urls.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise HTTPException(400, "No URLs found in the list")

    # If zipping is requested, ensure we have a subfolder to group them in
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

    # Launch background zip task if requested
    if batch.zip_when_done:
        zip_job_id = uuid.uuid4().hex[:10]
        zip_job = Job(zip_job_id, f"Zip Process: {subfolder}.zip", Mode.ZIP_TASK, {})
        with JOBS_LOCK:
            JOBS[zip_job_id] = zip_job
            
        threading.Thread(
            target=watch_batch_and_zip, 
            args=(job_ids, zip_job_id, subfolder, batch.zip_password), 
            daemon=True
        ).start()
        created.append(zip_job.to_dict())

    return {"count": len(created), "jobs": created}


@app.get("/api/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs]


@app.delete("/api/jobs/clear-finished")
def clear_finished():
    removed = 0
    with JOBS_LOCK:
        finished_ids = [
            jid for jid, j in JOBS.items()
            if j.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)
        ]
        for jid in finished_ids:
            del JOBS[jid]
            removed += 1
    return {"removed": removed}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job.cancel_requested = True
        return {"status": "cancel_requested"}
    else:
        with JOBS_LOCK:
            del JOBS[job_id]
        return {"status": "removed"}


@app.put("/api/jobs/{job_id}/retry")
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
# Frontend (static files)
# --------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(APP_DIR / "static"), html=True), name="static")