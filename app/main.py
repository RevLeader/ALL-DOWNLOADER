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
import shutil
import uuid
import threading
import time
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict

import yt_dlp
from fastapi import FastAPI, HTTPException, Depends, Request, Response, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import init_db, get_db, JobRecord
from app.storage import upload_file, delete_local_file, get_presigned_download_url, delete_from_bucket, UploadFailedError
from app.auth import (
    check_passphrase,
    create_session_token,
    require_login,
    is_logged_in,
    get_user_id,
    get_user_id_from_token,
    COOKIE_NAME,
    COOKIE_MAX_AGE,
)

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).parent
DOWNLOADS_DIR = APP_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Cookies directory — checked automatically based on the URL's domain.
# On Render: upload cookie files as Secret Files under Environment → Secret Files.
# Locally: put them in a cookies/ folder next to main.py (add cookies/ to .gitignore).
# File naming convention:
#   youtube.txt   → used for youtube.com, youtu.be
#   x.txt         → used for x.com, twitter.com
#   facebook.txt  → used for facebook.com, fb.watch
#   instagram.txt → used for instagram.com
# Add more as needed following the same pattern.
COOKIES_DIR_CANDIDATES = [
    Path("/etc/secrets"),          # Render Secret Files location
    APP_DIR / "cookies",           # local dev folder
]
COOKIES_DIR = next((p for p in COOKIES_DIR_CANDIDATES if p.exists()), None)

DOMAIN_COOKIE_MAP = {
    "youtube.com":   "youtube.txt",
    "youtu.be":      "youtube.txt",
    "x.com":         "x.txt",
    "twitter.com":   "x.txt",
    "facebook.com":  "facebook.txt",
    "fb.watch":      "facebook.txt",
    "instagram.com": "instagram.txt",
}

def auto_cookie_file(url: str) -> Optional[str]:
    """
    Returns the path to a cookies file for the given URL's domain if one
    exists on disk, otherwise None. Called automatically for every download
    so the user never has to think about it.

    Priority order:
      1. Check the writable DOWNLOADS_DIR first — this is where cookies
         uploaded via /api/cookies/upload endpoint are saved.
      2. Fall back to the read-only COOKIES_DIR (e.g. /etc/secrets or
         local cookies/ folder) and copy to downloads/ for yt-dlp's
         potential cookie-rotation rewrites.
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Strip www. prefix so www.youtube.com matches youtube.com
        host = host.removeprefix("www.")
        filename = DOMAIN_COOKIE_MAP.get(host)
        if not filename:
            return None

        # Priority 1: writable download dir (uploaded via API)
        writable = DOWNLOADS_DIR / filename
        if writable.exists():
            return str(writable)

        # Priority 2: read-only cookies dir (secret files / local folder)
        if COOKIES_DIR:
            full_path = COOKIES_DIR / filename
            if full_path.exists():
                # Copy to writable dir so yt-dlp can rewrite it if needed
                shutil.copy2(str(full_path), str(writable))
                return str(writable)

        return None
    except Exception:
        return None

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
    def __init__(self, job_id: str, user_id: str, url: str, mode: Mode, options: dict):
        self.id = job_id
        self.user_id = user_id
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
        self.upload_failed = False          # True when R2 upload failed but file is still on disk
        self.local_path: Optional[str] = None  # disk path for local-fallback download
        self._logger: Optional["JobLogger"] = None

    def add_log(self, line: str):
        self.log.append(line)
        if len(self.log) > 300:
            self.log = self.log[-300:]

    def to_dict(self):
        opts = self.options or {}
        return {
            "id": self.id,
            "url": self.url,
            "display_title": opts.get("display_title"),
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
            "upload_failed": self.upload_failed,
        }

    def persist(self, db: Session):
        """Upsert this job's current state into Postgres."""
        record = db.get(JobRecord, self.id)
        if record is None:
            record = JobRecord(id=self.id, user_id=self.user_id)
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
        job = cls(record.id, record.user_id, record.url, record.mode, record.options or {})
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
    url: Optional[str] = Field(None, description="Video/playlist URL")
    search_query: Optional[str] = Field(None, description="YouTube search query (song name, artist, etc.)")
    mode: Mode = Mode.MP4
    resolution: Optional[int] = None
    embed_thumbnail: bool = True
    add_metadata: bool = True
    output_template: Optional[str] = None
    subfolder: Optional[str] = None
    playlist_start: Optional[int] = None
    playlist_end: Optional[int] = None
    # Legacy fields — ignored; kept so older clients don't 422
    search_first: bool = False
    search_count: int = 1
    filter_keywords: Optional[str] = None
    user_agent: Optional[str] = None
    cookies_file: Optional[str] = None
    use_archive: bool = True
    raw_args: Optional[str] = None

    def effective_input(self) -> tuple[str, bool]:
        """Returns (input_text, is_search)."""
        search = (self.search_query or "").strip()
        if search:
            return search, True
        if self.search_first and (self.url or "").strip():
            return (self.url or "").strip(), True
        link = (self.url or "").strip()
        if not link:
            raise ValueError("Provide a video link or a YouTube search term.")
        return link, False


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

def safe_subfolder(subfolder: Optional[str]) -> str:
    """
    Sanitizes a user-supplied subfolder name so it can't escape DOWNLOADS_DIR
    (e.g. via '../../etc' or an absolute path). Strips path separators and
    '..' segments, keeping only a flat folder name.
    """
    if not subfolder:
        return ""
    # Take just the final path component, discarding any directory traversal.
    name = Path(subfolder).name
    if name in ("", ".", ".."):
        return ""
    return name


def build_ydl_opts(job: Job, req: DownloadRequest) -> dict:
    out_dir = DOWNLOADS_DIR / safe_subfolder(req.subfolder)
    out_dir.mkdir(parents=True, exist_ok=True)

    template = req.output_template or "%(title).100B.%(ext)s"
    if req.mode in (Mode.PLAYLIST, Mode.PLAYLIST_RANGE) and not req.output_template:
        template = "%(playlist_index)s - %(title).100B.%(ext)s"

    job_logger = JobLogger(job)
    job._logger = job_logger

    opts: dict = {
        "outtmpl": str(out_dir / template),
        "noplaylist": req.mode not in (Mode.PLAYLIST, Mode.PLAYLIST_RANGE),
        "progress_hooks": [lambda d: _progress_hook(job, d)],
        "postprocessor_hooks": [lambda d: _postprocessor_hook(job, d)],
        "logger": job_logger,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        # Hard backstop in case the title-field truncation above (.100B)
        # still isn't enough once yt-dlp appends extra text (e.g. playlist
        # index, uploader id). 120 chars leaves headroom under the 255-byte
        # filesystem limit even with multi-byte emoji in the remainder.
        "trim_file_name": 120,

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
        if req.resolution:
            # Try the requested height first, then progressively fall back to lower
            # resolutions rather than erroring when the exact height isn't available
            # (e.g. a reel that only offers 720p when 1080p was requested).
            h = req.resolution
            fallbacks = [h, 1080, 720, 480, 360] if h > 360 else [h, 360]
            # Build a chain: bestvideo[height<=X]+bestaudio / best[height<=X]
            # for each fallback height, deduplicated while preserving order.
            seen = set()
            parts = []
            for fh in fallbacks:
                if fh not in seen:
                    seen.add(fh)
                    parts.append(
                        f"bestvideo[height<={fh}]+bestaudio[ext=m4a]/"
                        f"bestvideo[height<={fh}]+bestaudio/"
                        f"best[height<={fh}]"
                    )
            parts.append("bestvideo+bestaudio/best")  # final catch-all
            opts["format"] = "/".join(parts)
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

    if req.mode == Mode.FACEBOOK or req.user_agent:
        opts["http_headers"] = {
            "User-Agent": req.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

    if req.mode == Mode.X and not req.output_template:
        opts["outtmpl"] = str(out_dir / "%(uploader_id)s_%(id)s.%(ext)s")

    # ── YouTube OAuth / Cookie auto-injection ──
    # Priority: OAuth token > uploaded cookies > secret files > nothing
    #
    # OAuth token (youtube_oauth.json) lasts months and is the best option.
    # It's generated by running locally:
    #   yt-dlp --username oauth --password "" --oauth-cache youtube_oauth.json
    # Then upload the resulting file via the admin cookies endpoint.
    # Once uploaded, it's used automatically for every YouTube download.
    #
    # The cookies file is the fallback auth method for sites that don't
    # support OAuth (e.g. X/Twitter, Facebook, Instagram).
    if "youtube.com" in job.url or "youtu.be" in job.url:
        oauth_token_path = DOWNLOADS_DIR / "youtube_oauth.json"
        if oauth_token_path.exists():
            opts["username"] = "oauth"
            opts["password"] = ""
            opts["oauth_cache"] = str(oauth_token_path)
            opts["no_warnings"] = True
            job.add_log("Using YouTube OAuth login.")
        else:
            # Fall back to cookies for YouTube
            resolved_cookie = auto_cookie_file(job.url)
            if resolved_cookie:
                opts["cookiefile"] = resolved_cookie
                opts["no_warnings"] = True
                job.add_log("Using saved login for this site.")
    else:
        # Non-YouTube sites: use cookies if available
        resolved_cookie = auto_cookie_file(job.url)
        if resolved_cookie:
            opts["cookiefile"] = resolved_cookie
            opts["no_warnings"] = True
            job.add_log("Using saved login for this site.")
        elif req.cookies_file:
            cookie_path = Path(req.cookies_file)
            if cookie_path.exists():
                opts["cookiefile"] = str(cookie_path)
                opts["no_warnings"] = True
            else:
                job.add_log(f"Warning: cookies file '{req.cookies_file}' not found, continuing without it.")

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
        # Suppress noisy-but-harmless cookie warnings (expired entries, missing
        # columns, etc.) — they don't affect the download and just confuse users.
        cookie_noise = (
            "Couldn't decrypt cookie",
            "unable to open database",
            "KeyError: 'expirationDate'",
            "cookie",         # broad — catches "cookies file", "cookie jar", etc.
            "sqlite",
        )
        if any(kw.lower() in msg.lower() for kw in cookie_noise):
            return   # swallow silently
        if "Retrying" in msg or "Giving up after" in msg:
            self.job.add_log(f"Network issue/interruption: {msg} (Will keep retrying)")
        else:
            self.job.add_log(f"Warning: {msg}")

    def error(self, msg):
        if "max-downloads" in msg or "Maximum number of downloads reached" in msg:
            self.job.add_log(f"Stopped after first match (by design): {msg}")
            return
        # Suppress cookie-related errors — they're usually non-fatal (expired
        # session, missing file) and yt-dlp will continue without them.
        cookie_noise = (
            "Couldn't decrypt cookie",
            "unable to open database",
            "cookie",
            "sqlite",
        )
        if any(kw.lower() in msg.lower() for kw in cookie_noise):
            self.job.add_log(f"Note: cookie issue (continuing without saved login): {msg}")
            return   # don't mark job as errored
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
        # NOTE: this filename is a snapshot taken *during* download, before
        # postprocessing (merge, thumbnail embed, metadata) runs. It can be
        # wrong by the time the job finishes — e.g. postprocessing remuxes
        # a .webm fragment into .mp4, or falls back to .m4a if no video
        # stream merged. We still set it here for live progress display,
        # but the postprocessor hook below overwrites it with the real
        # final filename once processing actually completes.
        fname = d.get("filename")
        if fname:
            job.filename = os.path.basename(fname)

    elif d["status"] == "finished":
        job.progress = 100.0
        job.add_log("Finished downloading, now processing (thumbnail/metadata)...")


def _postprocessor_hook(job: Job, d: dict):
    """
    Fires after each postprocessor step (merge, thumbnail embed, metadata).
    'filepath' here is yt-dlp's own authoritative answer for the file's
    current location/name — more trustworthy than the download-time
    snapshot from _progress_hook, since postprocessing can change the
    extension or filename after that snapshot was taken.
    """
    if d.get("status") == "finished":
        info = d.get("info_dict") or {}
        filepath = info.get("filepath") or d.get("filepath")
        if filepath:
            job.filename = os.path.basename(filepath)
        title = info.get("title")
        if title:
            job.options = dict(job.options or {})
            if not job.options.get("display_title"):
                job.options["display_title"] = title


# --------------------------------------------------------------------------
# Workers (Download, Upload-to-R2, & Zip)
# --------------------------------------------------------------------------

def _find_recently_written_file(out_dir: Path, since: datetime) -> Optional[Path]:
    """
    Looks for a file in out_dir written after the job started.
    When several candidates exist, returns the newest one.
    """
    if not out_dir.exists():
        return None
    since_ts = since.timestamp() - 2  # small clock slack
    skip_names = {".part", ".ytdl", ".tmp"}
    candidates = [
        f for f in out_dir.iterdir()
        if f.is_file()
        and f.stat().st_mtime >= since_ts
        and f.name != "archive.txt"
        and not any(f.name.endswith(ext) for ext in skip_names)
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda f: f.stat().st_mtime)


def _locate_output_file(job: "Job", out_dir: Path) -> Optional[Path]:
    """Find the finished download on disk using tracked name or mtime fallback."""
    if job.filename:
        candidate = out_dir / job.filename
        if candidate.exists():
            return candidate
    fallback = _find_recently_written_file(out_dir, job.created_at)
    if fallback:
        job.filename = fallback.name
    return fallback


def _was_archive_skipped(job: "Job") -> bool:
    markers = (
        "already been recorded in the archive",
        "has already been downloaded",
        "already downloaded",
    )
    haystack = " ".join(job.log).lower()
    return any(m in haystack for m in markers)


def _resolve_youtube_search(query: str, cookiefile: Optional[str] = None) -> dict:
    """
    Search YouTube and return the top result's URL and title.
    Uses ytsearch1 so we always pick the best-ranked official match.
    """
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)

    entries = info.get("entries") or []
    if not entries:
        raise yt_dlp.utils.DownloadError(f'No YouTube results for "{query}"')

    entry = entries[0]
    video_id = entry.get("id")
    url = entry.get("webpage_url") or entry.get("url")
    if not url and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    title = entry.get("title") or query
    if not url:
        raise yt_dlp.utils.DownloadError(f'Could not resolve a video URL for "{query}"')

    return {"url": url, "title": title, "id": video_id}


def _prepare_download_request(job: "Job", req: DownloadRequest) -> tuple[DownloadRequest, str]:
    """
    Resolves YouTube search queries to a concrete video URL and stores the
    real YouTube title on the job for display.
    Returns (updated_request, download_url).
    """
    try:
        input_text, is_search = req.effective_input()
    except ValueError as exc:
        raise yt_dlp.utils.DownloadError(str(exc)) from exc

    download_url = input_text
    if is_search:
        job.add_log(f'Searching YouTube for: "{input_text}"')
        cookiefile = auto_cookie_file("https://www.youtube.com/")
        resolved = _resolve_youtube_search(input_text, cookiefile=cookiefile)
        download_url = resolved["url"]
        job.url = download_url
        job.options = dict(job.options or {})
        job.options["display_title"] = resolved["title"]
        job.options["search_query"] = input_text
        job.add_log(f'Found: {resolved["title"]}')
        _persist(job)

    return req, download_url


def _upload_or_fallback(job: "Job", local_path: str):
    """
    Tries to upload local_path to R2 (storage.py retries up to 4× internally).
    On success: marks job download-ready, deletes local file.
    On UploadFailedError: marks job download-ready with upload_failed=True so
    the frontend can still offer a direct-from-disk download while the file
    exists (until next server restart).
    """
    try:
        storage_key = upload_file(local_path, job.id, job.filename)
        job.storage_key = storage_key
        job.download_ready = True
        job.upload_failed = False
        job.local_path = None
        job.add_log("Upload complete. File ready to download.")
        delete_local_file(local_path)
    except UploadFailedError as upload_err:
        # Keep job as DONE — still show Download button, but stream locally
        job.download_ready = True
        job.upload_failed = True
        job.local_path = local_path
        job.add_log(
            f"⚠ Cloud upload failed ({upload_err}). "
            "File can still be downloaded directly from this server — "
            "do it now, before the server restarts."
        )


def _claim_legacy_jobs(user_id: str):
    """
    Called once per new login session: reassigns any user_id='legacy' rows
    (created before the per-browser identity system existed) to the new uid
    so they appear in that person's job history.  Safe on every login — a
    no-op when no legacy rows exist.
    """
    db = next(get_db())
    try:
        from sqlalchemy import text
        result = db.execute(
            text("UPDATE jobs SET user_id = :uid WHERE user_id = 'legacy'"),
            {"uid": user_id},
        )
        db.commit()
        if result.rowcount:
            # Pull the newly claimed records into the in-memory JOBS map
            records = db.query(JobRecord).filter_by(user_id=user_id).all()
            with JOBS_LOCK:
                for rec in records:
                    if rec.id not in JOBS:
                        JOBS[rec.id] = Job.from_record(rec)
    except Exception:
        pass   # not fatal
    finally:
        db.close()


def run_job(job: Job, req: DownloadRequest):
    job.status = JobStatus.RUNNING
    job.add_log(f"Starting [{job.mode}] for: {job.url}")
    _persist(job)

    try:
        req, download_url = _prepare_download_request(job, req)
        opts = build_ydl_opts(job, req)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([download_url])

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

        out_dir = DOWNLOADS_DIR / safe_subfolder(req.subfolder)
        local_path = _locate_output_file(job, out_dir)

        if not local_path:
            if _was_archive_skipped(job):
                job.status = JobStatus.ERROR
                job.error = (
                    "This video was skipped because it was downloaded before. "
                    "Uncheck “Skip videos already downloaded” to download it again."
                )
            else:
                job.status = JobStatus.ERROR
                job.error = "Download finished but no output file was found."
            job.add_log(f"Error: {job.error}")
            _persist(job)
            return

        job.status = JobStatus.UPLOADING
        job.add_log("Uploading finished file to cloud storage...")
        _persist(job)
        _upload_or_fallback(job, str(local_path))

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
        import pyzipper # pyright: ignore[reportMissingImports]

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
        zjob.filename = zip_path.name
        _persist(zjob)

        _upload_or_fallback(zjob, str(zip_path))

        zjob.status = JobStatus.DONE
        zjob.add_log(f"Done! Created encrypted zip: {zip_path.name}")
        _persist(zjob)
    except Exception as e:
        zjob.status = JobStatus.ERROR
        zjob.error = f"Zipping failed: {e}"
        zjob.add_log(f"Error zipping: {e}")
        _persist(zjob)


# --------------------------------------------------------------------------
# Keep-alive pinger — prevents Render's free tier from spinning down
# after 15 minutes of inactivity. Pings its own /api/health endpoint
# every 10 minutes so the service stays warm.
#
# NOTE: This only works if the service is already running. If Render
# spins it down completely, the thread stops too. For true 24/7 "wake",
# pair this with an *external* cron job:
#   cron-job.org (free) → https://yourapp.onrender.com/api/health
# every 10 minutes. The external pinger wakes a sleeping instance; the
# internal one keeps it awake once it's up.
# --------------------------------------------------------------------------

KEEPALIVE_INTERVAL = 600  # 10 minutes in seconds


def _keepalive_pinger():
    """Background thread that pings the app's health endpoint."""
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            # Use urllib (stdlib) instead of httpx to avoid adding deps
            import urllib.request
            port = os.environ.get("PORT", "10000")
            url = f"http://localhost:{port}/api/health"
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass  # silently retry next cycle


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()

    # Start keep-alive pinger daemon thread
    threading.Thread(target=_keepalive_pinger, daemon=True).start()

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

    # Reassign any pre-identity-system 'legacy' job rows to this browser's
    # new uid so they appear in their history (runs in background — non-blocking).
    uid = get_user_id_from_token(token)
    threading.Thread(target=_claim_legacy_jobs, args=(uid,), daemon=True).start()

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
def create_job(req: DownloadRequest, request: Request):
    user_id = get_user_id(request)
    try:
        input_text, is_search = req.effective_input()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    job_id = uuid.uuid4().hex[:10]
    initial_url = input_text if not is_search else f'search: {input_text}'
    job = Job(job_id, user_id, initial_url, req.mode, req.model_dump())

    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist(job)

    thread = threading.Thread(target=run_job, args=(job, req), daemon=True)
    thread.start()

    return job.to_dict()


@app.post("/api/jobs/batch", dependencies=[Depends(require_login)])
def create_batch(batch: BatchRequest, request: Request):
    user_id = get_user_id(request)
    lines = [
        line.strip()
        for line in batch.urls.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        raise HTTPException(400, "No URLs found in the list")

    subfolder = safe_subfolder(batch.subfolder)
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
        j = create_job(req, request)
        created.append(j)
        job_ids.append(j["id"])

    if batch.zip_when_done:
        zip_job_id = uuid.uuid4().hex[:10]
        zip_job = Job(zip_job_id, user_id, f"Zip Process: {subfolder}.zip", Mode.ZIP_TASK, {})
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
def list_jobs(request: Request):
    user_id = get_user_id(request)
    with JOBS_LOCK:
        jobs = sorted(
            (j for j in JOBS.values() if j.user_id == user_id),
            key=lambda j: j.created_at, reverse=True,
        )
        return [j.to_dict() for j in jobs]


@app.delete("/api/jobs/clear-finished", dependencies=[Depends(require_login)])
def clear_finished(request: Request, db: Session = Depends(get_db)):
    user_id = get_user_id(request)
    removed = 0
    with JOBS_LOCK:
        finished_ids = [
            jid for jid, j in JOBS.items()
            if j.user_id == user_id and j.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)
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


def _get_owned_job(job_id: str, user_id: str) -> "Job":
    """
    Fetches a job only if it belongs to user_id. Returns 404 (not 403) for
    a job that exists but belongs to someone else — same response as a
    job_id that doesn't exist at all, so this never confirms or denies
    that a given job_id belongs to another user.
    """
    job = JOBS.get(job_id)
    if not job or job.user_id != user_id:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_login)])
def get_job(job_id: str, request: Request):
    job = _get_owned_job(job_id, get_user_id(request))
    return job.to_dict()


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_login)])
def get_download_url(job_id: str, request: Request):
    """
    Returns a pre-signed R2 URL (valid ~1hr) for the finished file.
    If the R2 upload failed but the file is still on local disk, returns
    local_only=True so the frontend hits /download-local instead.
    """
    job = _get_owned_job(job_id, get_user_id(request))
    if not job.download_ready:
        raise HTTPException(409, "File isn't ready yet")

    if getattr(job, "upload_failed", False):
        # File never made it to R2 — stream directly from local disk
        local_path = getattr(job, "local_path", None)
        if not local_path or not os.path.exists(local_path):
            raise HTTPException(
                410,
                "File is no longer available (server was restarted before the cloud upload succeeded). "
                "Please retry the job."
            )
        return {
            "local_only": True,
            "warning": "Cloud upload failed — file is available directly from this server until the next restart.",
        }

    if not job.storage_key:
        raise HTTPException(409, "File isn't ready yet")

    url = get_presigned_download_url(job.storage_key, filename=job.filename, expires_in=3600)
    return {"url": url, "expires_in": 3600, "local_only": False}


@app.get("/api/jobs/{job_id}/download-local", dependencies=[Depends(require_login)])
def download_local(job_id: str, request: Request):
    """
    Streams the file directly from Render's local disk when the R2 upload
    failed. Only available until the next server restart — after that the
    file is gone and the user should retry the job.
    """
    job = _get_owned_job(job_id, get_user_id(request))
    local_path = getattr(job, "local_path", None)
    if not local_path or not os.path.exists(local_path):
        raise HTTPException(
            410,
            "File is no longer available locally (server was restarted). "
            "Please retry the download job."
        )
    return FileResponse(
        local_path,
        filename=job.filename or "download",
        media_type="application/octet-stream",
    )


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_login)])
def delete_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = _get_owned_job(job_id, get_user_id(request))

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
def retry_job(job_id: str, request: Request):
    old = _get_owned_job(job_id, get_user_id(request))
    opts = dict(old.options or {})
    field_names = DownloadRequest.model_fields.keys()
    clean = {k: v for k, v in opts.items() if k in field_names}
    if clean.get("search_query"):
        clean["url"] = None
    elif not clean.get("url"):
        clean["url"] = old.url
    req = DownloadRequest(**clean)
    return create_job(req, request)


# --------------------------------------------------------------------------
# Cookie management — upload fresh cookies and check status
# --------------------------------------------------------------------------

# Path where uploaded/temporary cookies are stored (writable)
COOKIES_WRITABLE_DIR = DOWNLOADS_DIR
COOKIES_WRITABLE_DIR.mkdir(exist_ok=True)

# Map: domain -> filename in COOKIES_WRITABLE_DIR
# Same as DOMAIN_COOKIE_MAP but these are the writable copies
COOKIE_EXTRA_INFO_FILE = COOKIES_WRITABLE_DIR / "_cookie_meta.json"

def _load_cookie_meta() -> dict:
    """Load metadata about when each cookie file was last uploaded."""
    try:
        if COOKIE_EXTRA_INFO_FILE.exists():
            return json.loads(COOKIE_EXTRA_INFO_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_cookie_meta(meta: dict):
    """Save metadata about cookie upload times."""
    try:
        COOKIE_EXTRA_INFO_FILE.write_text(json.dumps(meta, indent=2, default=str))
    except Exception:
        pass

@app.post("/api/cookies/upload", dependencies=[Depends(require_login)])
async def upload_cookies(
    request: Request,
    file: UploadFile = File(...),
    domain: str = Form("youtube.com"),
):
    """
    Upload a fresh cookies file for a specific domain.
    The file is saved to the writable cookies directory and replaces
    the old cookie file for that domain.

    - file: the cookies.txt / Netscape format cookie file
    - domain: which site this is for (youtube.com, x.com, facebook.com, instagram.com)

    Returns the domain, filename, and a note.
    """
    if file.filename is None:
        raise HTTPException(400, "No filename in upload")

    # Map the domain to the expected filename
    domain_lower = domain.lower().strip()
    cookie_filename = DOMAIN_COOKIE_MAP.get(domain_lower)
    if not cookie_filename:
        # Allow uploading custom cookie files too
        valid_domains = ", ".join(sorted(set(DOMAIN_COOKIE_MAP.values())))
        raise HTTPException(
            400,
            f"Unknown domain '{domain}'. Supported domains map to: {valid_domains}. "
            f"Or pass a domain key like: youtube.com, x.com, facebook.com, instagram.com"
        )

    dest_path = COOKIES_WRITABLE_DIR / cookie_filename
    content = await file.read()
    dest_path.write_bytes(content)

    # Record upload time
    meta = _load_cookie_meta()
    meta[domain_lower] = {
        "uploaded_at": datetime.utcnow().isoformat(),
        "filename": cookie_filename,
        "size_bytes": len(content),
    }
    _save_cookie_meta(meta)

    return {
        "ok": True,
        "domain": domain_lower,
        "filename": cookie_filename,
        "path": str(dest_path),
        "size_bytes": len(content),
        "note": "Cookie file saved. It will be used automatically for future downloads from this domain."
    }


@app.get("/api/cookies/status", dependencies=[Depends(require_login)])
def cookie_status():
    """
    Returns info about all known cookie files: which domains have cookies,
    when they were last uploaded, and if they look like they might be expired.
    """
    meta = _load_cookie_meta()
    result = {}
    now = datetime.utcnow()

    for domain, cookie_filename in DOMAIN_COOKIE_MAP.items():
        info = meta.get(domain, {})

        # Check if file exists in either writable dir or original cookies dir
        writable_path = COOKIES_WRITABLE_DIR / cookie_filename
        orig_path = (COOKIES_DIR / cookie_filename) if COOKIES_DIR else None
        exists = writable_path.exists() or (orig_path and orig_path.exists())

        uploaded_at_str = info.get("uploaded_at")
        uploaded_at = None
        age_days = None
        expired = None

        if uploaded_at_str:
            try:
                uploaded_at = datetime.fromisoformat(uploaded_at_str)
                age_days = (now - uploaded_at).days
                # Cookies typically expire after ~7-30 days depending on site
                # Flag as "expiring" after 7 days, "expired" after 14
                if age_days >= 14:
                    expired = "expired"
                elif age_days >= 7:
                    expired = "expiring_soon"
                else:
                    expired = "fresh"
            except Exception:
                pass

        result[domain] = {
            "exists": exists,
            "filename": cookie_filename,
            "uploaded_at": uploaded_at_str,
            "age_days": age_days,
            "status": expired or ("no_file" if not exists else "unknown"),
        }

    return result


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
    public_paths = ("/login", "/api/login", "/api/logout", "/api/auth/status", "/api/health", "/api/cookies/upload", "/api/cookies/status")
    is_static_asset = path.startswith("/static/") or path in ("/favicon.ico",)

    if path in public_paths or is_static_asset:
        return await call_next(request)

    if not is_logged_in(request):
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Not logged in"})
        return RedirectResponse(url="/login")

    return await call_next(request)


app.mount("/", StaticFiles(directory=str(APP_DIR / "static"), html=True), name="static")