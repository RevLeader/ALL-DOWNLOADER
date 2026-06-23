"""
cookie_refresher.py
--------------------
Background service that keeps YouTube cookies fresh automatically.
"""

import os
import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cookie_refresher")
logger.setLevel(logging.INFO)

if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# ── Configuration ─────────────────────────────────────────────────────────────

REFRESH_INTERVAL_HOURS = float(os.environ.get("COOKIE_REFRESH_HOURS", "6"))
STALE_AFTER_HOURS      = float(os.environ.get("COOKIE_STALE_HOURS", "12"))
COOKIE_BROWSER         = os.environ.get("COOKIE_BROWSER", "chrome").lower()
COOKIE_BROWSER_PROFILE = os.environ.get("COOKIE_BROWSER_PROFILE", "")
USE_OAUTH2             = os.environ.get("USE_OAUTH2", "").lower() in ("1", "true", "yes")

YOUTUBE_COOKIE_FILE = "youtube.txt"

def _meta_path(cookies_dir: Path) -> Path:
    return cookies_dir / "_cookie_meta.json"

def _load_meta(cookies_dir: Path) -> dict:
    try:
        p = _meta_path(cookies_dir)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}

def _save_meta(cookies_dir: Path, meta: dict):
    try:
        _meta_path(cookies_dir).write_text(json.dumps(meta, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Could not save cookie meta: {e}")

def _cookie_age_hours(cookies_dir: Path) -> Optional[float]:
    meta = _load_meta(cookies_dir)
    info = meta.get("youtube.com", {})
    uploaded_at_str = info.get("uploaded_at")
    if uploaded_at_str:
        try:
            uploaded_at = datetime.fromisoformat(uploaded_at_str)
            if uploaded_at.tzinfo is None:
                uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - uploaded_at).total_seconds() / 3600
        except Exception:
            pass
    cookie_path = cookies_dir / YOUTUBE_COOKIE_FILE
    if cookie_path.exists():
        age_secs = time.time() - cookie_path.stat().st_mtime
        return age_secs / 3600
    return None

def _update_meta(cookies_dir: Path, strategy: str, size_bytes: int):
    meta = _load_meta(cookies_dir)
    meta["youtube.com"] = {
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "filename": YOUTUBE_COOKIE_FILE,
        "size_bytes": size_bytes,
        "refresh_strategy": strategy,
        "auto_refreshed": True,
    }
    _save_meta(cookies_dir, meta)

def _try_browser_extraction(cookies_dir: Path) -> bool:
    dest = cookies_dir / YOUTUBE_COOKIE_FILE
    tmp  = cookies_dir / "_youtube_tmp.txt"
    browser_arg = COOKIE_BROWSER
    if COOKIE_BROWSER_PROFILE:
        browser_arg = f"{COOKIE_BROWSER}:{COOKIE_BROWSER_PROFILE}"

    cmd = [
        "yt-dlp",
        "--cookies-from-browser", browser_arg,
        "--cookies", str(tmp),
        "--skip-download",
        "--quiet",
        "--no-warnings",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    ]
    logger.info(f"[Strategy A] Extracting cookies from {COOKIE_BROWSER}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if tmp.exists() and tmp.stat().st_size > 200:
            shutil.move(str(tmp), str(dest))
            size = dest.stat().st_size
            _update_meta(cookies_dir, f"browser:{COOKIE_BROWSER}", size)
            logger.info(f"[Strategy A] ✓ Cookies refreshed ({size} bytes)")
            return True
        logger.warning(f"[Strategy A] No usable cookies from browser.")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.warning(f"[Strategy A] Error: {e}")
        return False

def _try_oauth2_extraction(cookies_dir: Path) -> bool:
    dest = cookies_dir / YOUTUBE_COOKIE_FILE
    tmp  = cookies_dir / "_youtube_oauth2_tmp.txt"

    cmd = [
        "yt-dlp",
        "--username", "oauth2",
        "--password", "",
        "--cookies", str(tmp),
        "--skip-download",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    ]

    print("\n" + "="*60, flush=True)
    print("🎬 [Strategy B] Launching OAuth2 device-code flow...", flush=True)
    print("⏳ WATCH THIS LOG FOR THE GOOGLE AUTHORIZATION URL!", flush=True)
    print("="*60 + "\n", flush=True)

    try:
        # Stream the output live so we don't have to wait to see the auth link
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in iter(process.stdout.readline, ''):
            print(f"[yt-dlp] {line.strip()}", flush=True)

        process.stdout.close()
        process.wait(timeout=360)

        if tmp.exists() and tmp.stat().st_size > 200:
            shutil.move(str(tmp), str(dest))
            size = dest.stat().st_size
            _update_meta(cookies_dir, "oauth2", size)
            print(f"\n✅ [Strategy B] OAuth2 cookies written ({size} bytes)\n", flush=True)
            return True

        print("\n❌ [Strategy B] OAuth2 produced no usable cookie file.\n", flush=True)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False
    except Exception as e:
        print(f"\n❌ [Strategy B] Error: {e}\n", flush=True)
        return False

def refresh_cookies(cookies_dir: Path, force: bool = False) -> bool:
    age = _cookie_age_hours(cookies_dir)
    if not force:
        if age is not None and age < STALE_AFTER_HOURS:
            logger.info(f"Cookies are {age:.1f}h old (< {STALE_AFTER_HOURS}h). Skipping.")
            return True

    logger.info(f"Refreshing YouTube cookies (age={age}, force={force})...")
    if not USE_OAUTH2:
        if _try_browser_extraction(cookies_dir):
            return True

    if _try_oauth2_extraction(cookies_dir):
        return True

    cookie_path = cookies_dir / YOUTUBE_COOKIE_FILE
    if cookie_path.exists():
        logger.warning("All auto-refresh strategies failed. Keeping existing cookie.")
    else:
        logger.error("All auto-refresh strategies failed and no cookie file exists.")
    return False

class CookieRefresher:
    def __init__(self, cookies_dir: Path):
        self.cookies_dir = cookies_dir
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._force_refresh_event = threading.Event()
        self._last_refresh: Optional[datetime] = None
        self._refresh_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="cookie-refresher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def force_refresh(self):
        self._force_refresh_event.set()

    def _loop(self):
        self._stop_event.wait(timeout=30)
        if self._stop_event.is_set():
            return

        while not self._stop_event.is_set():
            forced = self._force_refresh_event.is_set()
            if forced:
                self._force_refresh_event.clear()

            with self._refresh_lock:
                try:
                    success = refresh_cookies(self.cookies_dir, force=forced)
                    if success:
                        self._last_refresh = datetime.now(timezone.utc)
                except Exception as e:
                    logger.error(f"Cookie refresh loop error: {e}", exc_info=True)

            self._force_refresh_event.wait(timeout=REFRESH_INTERVAL_HOURS * 3600)

    def status(self) -> dict:
        age = _cookie_age_hours(self.cookies_dir)
        meta = _load_meta(self.cookies_dir)
        yt_meta = meta.get("youtube.com", {})
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "cookie_age_hours": round(age, 2) if age is not None else None,
            "cookie_file_exists": (self.cookies_dir / YOUTUBE_COOKIE_FILE).exists(),
            "cookie_size_bytes": yt_meta.get("size_bytes"),
            "refresh_strategy": yt_meta.get("refresh_strategy"),
            "auto_refreshed": yt_meta.get("auto_refreshed", False),
            "stale_after_hours": STALE_AFTER_HOURS,
            "refresh_interval_hours": REFRESH_INTERVAL_HOURS,
            "browser": COOKIE_BROWSER,
            "use_oauth2": USE_OAUTH2,
        }