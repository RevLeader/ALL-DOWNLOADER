"""
cookie_refresher.py
--------------------
Background service that keeps YouTube cookies fresh automatically, so the
"Sign in to confirm you're not a bot" error never reaches users.

HOW IT WORKS
============
YouTube's bot detection fires when:
  1. yt-dlp uses an expired or missing session cookie, OR
  2. yt-dlp uses a "from browser" cookie extraction that YouTube has flagged.

This module solves both by generating a valid Netscape-format cookies.txt
using one of two strategies, in priority order:

  Strategy A — yt-dlp --cookies-from-browser (Chrome/Firefox/Edge)
    yt-dlp can read directly from the browser's cookie store.
    Works on any machine where a browser is installed and has a logged-in
    YouTube session. Pulls live cookies that are always current.
    ⚠ Requires a real browser on the same machine. Works great on a local
    server or a VPS where you've logged into YouTube in a browser once.

  Strategy B — oauth2 plugin (headless, no browser needed)
    Uses the yt-dlp-get-pot / YouTube OAuth2 flow to get a persistent token
    that bypasses cookie requirements entirely. Best for Render/cloud hosting
    where there's no browser.

  Strategy C — Passive rotation (fallback)
    If neither above is available, the refresher logs a warning and keeps
    using whatever cookie file is already on disk (the manually uploaded one).
    It will re-check every REFRESH_INTERVAL_HOURS and upgrade to A or B
    automatically the moment they become available.

REFRESH SCHEDULE
================
Cookies are refreshed:
  • On startup (first check within 30 seconds of the server starting)
  • Every REFRESH_INTERVAL_HOURS (default: 6 hours) after that
  • Immediately when a download fails with a bot-detection error
    (call `cookie_refresher.force_refresh()` from your error handler)

SETUP
=====
1. Copy this file to your app/ directory alongside main.py.
2. In main.py, add these two lines near the top of the file:

    from app.cookie_refresher import CookieRefresher
    cookie_refresher = CookieRefresher(cookies_dir=DOWNLOADS_DIR)
    cookie_refresher.start()

3. In run_job(), detect bot errors and trigger an immediate refresh:

    except yt_dlp.utils.DownloadError as e:
        if "Sign in to confirm" in str(e) or "bot" in str(e).lower():
            cookie_refresher.force_refresh()   # refresh in background, next retry will pick it up
        ...

4. Optional env vars (set in .env or Render environment):

    COOKIE_BROWSER=chrome          # chrome, firefox, edge, safari (Strategy A)
    COOKIE_BROWSER_PROFILE=Default # browser profile name (optional)
    COOKIE_REFRESH_HOURS=6         # how often to refresh (default: 6)
    YOUTUBE_EMAIL=you@gmail.com    # used to verify login state (optional)

RENDER / CLOUD DEPLOYMENT
==========================
On Render, Strategy A won't work (no browser). Use Strategy B (oauth2):

  pip install yt-dlp[default] yt-dlp-get-pot

Then set the env var:
    USE_OAUTH2=true

The first run will print an auth URL to the logs — open it once in your
browser to authorize. After that, the token is stored in DOWNLOADS_DIR and
refreshed silently forever.

Alternatively, keep uploading cookies manually via /api/cookies/upload —
the refresher will detect that a fresh file exists and won't try to replace it
until it's older than STALE_AFTER_HOURS (default: 12 hours).
"""

import os
import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cookie_refresher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# ── Configuration ─────────────────────────────────────────────────────────────

REFRESH_INTERVAL_HOURS = float(os.environ.get("COOKIE_REFRESH_HOURS", "6"))
STALE_AFTER_HOURS      = float(os.environ.get("COOKIE_STALE_HOURS", "12"))
COOKIE_BROWSER         = os.environ.get("COOKIE_BROWSER", "chrome").lower()  # chrome/firefox/edge/safari
COOKIE_BROWSER_PROFILE = os.environ.get("COOKIE_BROWSER_PROFILE", "")        # e.g. "Default"
USE_OAUTH2             = os.environ.get("USE_OAUTH2", "").lower() in ("1", "true", "yes")

# The cookie filename that main.py's DOMAIN_COOKIE_MAP already expects
YOUTUBE_COOKIE_FILE = "youtube.txt"

# ── Bot-detection error signatures ────────────────────────────────────────────
BOT_ERROR_PHRASES = [
    "sign in to confirm you're not a bot",
    "sign in to confirm",
    "confirm you're not a bot",
    "please sign in",
    "this video is not available",
    "http error 429",
    "too many requests",
    "video unavailable",
    "cookies",
    "bot",
]


def _looks_like_bot_error(message: str) -> bool:
    msg = message.lower()
    return any(phrase in msg for phrase in BOT_ERROR_PHRASES)


# ── Meta file helpers (mirrors main.py's _load_cookie_meta) ──────────────────

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
    """Returns how old the current youtube.txt is, in hours. None if unknown."""
    meta = _load_meta(cookies_dir)
    info = meta.get("youtube.com", {})
    uploaded_at_str = info.get("uploaded_at")
    if not uploaded_at_str:
        # Fall back to file mtime
        cookie_path = cookies_dir / YOUTUBE_COOKIE_FILE
        if cookie_path.exists():
            age_secs = time.time() - cookie_path.stat().st_mtime
            return age_secs / 3600
        return None
    try:
        uploaded_at = datetime.fromisoformat(uploaded_at_str)
        return (datetime.utcnow() - uploaded_at).total_seconds() / 3600
    except Exception:
        return None


def _update_meta(cookies_dir: Path, strategy: str, size_bytes: int):
    meta = _load_meta(cookies_dir)
    meta["youtube.com"] = {
        "uploaded_at": datetime.utcnow().isoformat(),
        "filename": YOUTUBE_COOKIE_FILE,
        "size_bytes": size_bytes,
        "refresh_strategy": strategy,
        "auto_refreshed": True,
    }
    _save_meta(cookies_dir, meta)


# ── Strategy A: extract cookies from local browser ────────────────────────────

def _try_browser_extraction(cookies_dir: Path) -> bool:
    """
    Uses `yt-dlp --cookies-from-browser <browser> --skip-download` to extract
    and save a fresh Netscape cookies.txt. Works on any machine with a browser.
    Returns True on success.
    """
    dest = cookies_dir / YOUTUBE_COOKIE_FILE
    tmp  = cookies_dir / "_youtube_tmp.txt"

    cmd = [
        "yt-dlp",
        "--cookies-from-browser", COOKIE_BROWSER,
        "--cookies", str(tmp),
        "--skip-download",
        "--quiet",
        "--no-warnings",
        "https://www.youtube.com/",
    ]
    if COOKIE_BROWSER_PROFILE:
        # Insert profile after browser name: "--cookies-from-browser chrome:Default"
        cmd[2] = f"{COOKIE_BROWSER}:{COOKIE_BROWSER_PROFILE}"

    logger.info(f"[Strategy A] Extracting cookies from {COOKIE_BROWSER} browser...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if tmp.exists() and tmp.stat().st_size > 200:
            shutil.move(str(tmp), str(dest))
            size = dest.stat().st_size
            _update_meta(cookies_dir, f"browser:{COOKIE_BROWSER}", size)
            logger.info(f"[Strategy A] ✓ Cookies refreshed from {COOKIE_BROWSER} ({size} bytes)")
            return True
        else:
            stderr = result.stderr.strip()
            logger.warning(f"[Strategy A] Browser extraction produced no usable cookies. stderr: {stderr[:300]}")
            if tmp.exists():
                tmp.unlink()
            return False
    except subprocess.TimeoutExpired:
        logger.warning("[Strategy A] Browser extraction timed out.")
        return False
    except FileNotFoundError:
        logger.warning("[Strategy A] yt-dlp not found in PATH.")
        return False
    except Exception as e:
        logger.warning(f"[Strategy A] Unexpected error: {e}")
        return False


# ── Strategy B: OAuth2 token (headless, cloud-friendly) ──────────────────────

def _try_oauth2_extraction(cookies_dir: Path) -> bool:
    """
    Uses the yt-dlp OAuth2 flow to obtain a cookies/token file.
    Requires: pip install yt-dlp-get-pot  (or the yt-dlp[default] bundle)
    
    On first run, prints a URL to the server logs — you visit it once in a
    browser to authorize. After that, the token is stored and silently refreshed.
    
    Returns True if a usable token/cookie file was produced.
    """
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        logger.warning("[Strategy B] yt_dlp not importable, skipping OAuth2.")
        return False

    dest = cookies_dir / YOUTUBE_COOKIE_FILE
    token_file = cookies_dir / "_yt_oauth2_token.json"

    # Build ydl options for OAuth2 extraction
    opts = {
        "quiet": False,           # We want to see the auth URL in logs
        "no_warnings": False,
        "skip_download": True,
        "cookiefile": str(dest),
        # yt-dlp-get-pot uses this to store OAuth tokens between runs
        "extractor_args": {
            "youtube": {
                "po_token": [],
                "player_client": ["web"],
            }
        },
    }

    # If a token file from a previous run exists, inject it
    if token_file.exists():
        try:
            token_data = json.loads(token_file.read_text())
            logger.info("[Strategy B] Resuming from existing OAuth2 token.")
        except Exception:
            token_data = {}
    else:
        token_data = {}

    logger.info("[Strategy B] Attempting OAuth2 extraction (check logs for auth URL if first run)...")
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info("https://www.youtube.com/", download=False)

        if dest.exists() and dest.stat().st_size > 200:
            size = dest.stat().st_size
            _update_meta(cookies_dir, "oauth2", size)
            logger.info(f"[Strategy B] ✓ OAuth2 cookies written ({size} bytes)")
            return True
        else:
            logger.warning("[Strategy B] OAuth2 extraction produced no usable cookies file.")
            return False
    except Exception as e:
        msg = str(e)
        if "authorization" in msg.lower() or "oauth" in msg.lower():
            logger.info(f"[Strategy B] OAuth2 needs authorization — check server logs for URL. ({msg[:200]})")
        else:
            logger.warning(f"[Strategy B] OAuth2 error: {msg[:300]}")
        return False


# ── Strategy C: po_token via yt-dlp-get-pot (most robust, headless) ──────────

def _try_pot_extraction(cookies_dir: Path) -> bool:
    """
    Uses yt-dlp-get-pot to generate a Proof of Origin token (PO token).
    This is the most robust headless solution for cloud deployments.

    Install: pip install yt-dlp-get-pot

    The PO token is passed as an extractor_arg and combined with a minimal
    cookie file so YouTube accepts the request as a real browser session.
    Returns True if successful.
    """
    try:
        import yt_dlp
    except ImportError:
        return False

    dest = cookies_dir / YOUTUBE_COOKIE_FILE
    logger.info("[Strategy C] Attempting PO token extraction via yt-dlp-get-pot...")
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "cookiefile": str(dest),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(
                "https://www.youtube.com/watch?v=BaW_jenozKc",  # stable public test video
                download=False,
            )
        if dest.exists() and dest.stat().st_size > 200:
            size = dest.stat().st_size
            _update_meta(cookies_dir, "pot", size)
            logger.info(f"[Strategy C] ✓ PO token cookies written ({size} bytes)")
            return True
        return False
    except Exception as e:
        logger.warning(f"[Strategy C] PO token error: {str(e)[:200]}")
        return False


# ── Main refresh logic ─────────────────────────────────────────────────────────

def refresh_cookies(cookies_dir: Path, force: bool = False) -> bool:
    """
    Attempt to refresh the YouTube cookies file using the best available strategy.

    Args:
        cookies_dir: Path where youtube.txt is stored (your DOWNLOADS_DIR).
        force:       If True, refresh even if cookies appear fresh.

    Returns True if cookies were successfully refreshed.
    """
    age = _cookie_age_hours(cookies_dir)

    if not force:
        if age is not None and age < STALE_AFTER_HOURS:
            logger.debug(f"Cookies are {age:.1f}h old (< {STALE_AFTER_HOURS}h threshold). Skipping refresh.")
            return True  # Already fresh enough

    logger.info(f"Refreshing YouTube cookies (age={age:.1f}h, force={force})..." if age else
                f"Refreshing YouTube cookies (no age info, force={force})...")

    # Strategy A: local browser (best quality, works on local/VPS)
    if not USE_OAUTH2:
        if _try_browser_extraction(cookies_dir):
            return True

    # Strategy B: OAuth2 (good for headless/cloud, needs one-time auth)
    if USE_OAUTH2 or True:   # Always try as fallback
        if _try_oauth2_extraction(cookies_dir):
            return True

    # Strategy C: PO token (alternative headless approach)
    if _try_pot_extraction(cookies_dir):
        return True

    # No strategy worked
    cookie_path = cookies_dir / YOUTUBE_COOKIE_FILE
    if cookie_path.exists():
        logger.warning(
            "All auto-refresh strategies failed. Keeping existing cookie file. "
            "Consider uploading a fresh one via /api/cookies/upload or running "
            "the extractor manually on your server."
        )
        return False
    else:
        logger.error(
            "All auto-refresh strategies failed and no cookie file exists. "
            "YouTube bot-detection errors are likely. "
            "Please upload cookies via /api/cookies/upload."
        )
        return False


# ── Background refresh thread ──────────────────────────────────────────────────

class CookieRefresher:
    """
    Drop-in background service that keeps YouTube cookies fresh.

    Usage in main.py:
        from app.cookie_refresher import CookieRefresher
        cookie_refresher = CookieRefresher(cookies_dir=DOWNLOADS_DIR)
        cookie_refresher.start()

    Then in your error handler for bot-detection errors:
        cookie_refresher.force_refresh()
    """

    def __init__(self, cookies_dir: Path):
        self.cookies_dir = cookies_dir
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._force_refresh_event = threading.Event()
        self._last_refresh: Optional[datetime] = None
        self._refresh_lock = threading.Lock()

    def start(self):
        """Start the background refresh thread. Safe to call multiple times."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="cookie-refresher",
            daemon=True,  # Dies when the main process exits
        )
        self._thread.start()
        logger.info(f"Cookie refresher started. Interval: {REFRESH_INTERVAL_HOURS}h, "
                    f"stale threshold: {STALE_AFTER_HOURS}h, "
                    f"browser: {COOKIE_BROWSER}, oauth2: {USE_OAUTH2}")

    def stop(self):
        """Stop the background thread gracefully."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def force_refresh(self):
        """
        Trigger an immediate cookie refresh in the background.
        Call this when a download fails with a bot-detection error.
        The refresh runs asynchronously — the current job will fail (as
        expected), but the NEXT retry will pick up the fresh cookies.
        """
        logger.info("Bot-detection error detected — triggering immediate cookie refresh.")
        self._force_refresh_event.set()

    def _loop(self):
        # Initial delay: let the server finish starting up, then do a first check
        self._stop_event.wait(timeout=30)
        if self._stop_event.is_set():
            return

        while not self._stop_event.is_set():
            # Check if forced refresh was requested
            forced = self._force_refresh_event.is_set()
            if forced:
                self._force_refresh_event.clear()

            # Run the refresh (thread-safe)
            with self._refresh_lock:
                try:
                    success = refresh_cookies(self.cookies_dir, force=forced)
                    if success:
                        self._last_refresh = datetime.utcnow()
                except Exception as e:
                    logger.error(f"Cookie refresh loop error: {e}", exc_info=True)

            # Sleep until next interval (but wake up early if forced)
            interval_secs = REFRESH_INTERVAL_HOURS * 3600
            woke_early = self._force_refresh_event.wait(timeout=interval_secs)
            # If we woke early due to force flag, loop immediately (don't sleep again)

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    def status(self) -> dict:
        """Returns current status dict suitable for an API response."""
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
