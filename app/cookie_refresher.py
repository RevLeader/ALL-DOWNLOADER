"""
cookie_refresher.py
--------------------
Background service that keeps YouTube cookies fresh automatically, so the
"Sign in to confirm you're not a bot" error never reaches users.

HOW IT WORKS
============
Strategy A — yt-dlp --cookies-from-browser (Chrome/Firefox/Edge)
  Reads directly from the browser's live cookie store. Always current.
  ⚠ Requires a real browser on the same machine. Works on local or VPS.
  Set COOKIE_BROWSER=chrome (or firefox/edge/safari).

Strategy B — OAuth2 via yt-dlp's built-in device-code flow
  On first run, prints a Google device-auth URL to the server logs.
  Open it once in any browser, authorize, and the token persists forever.
  Best for Render/cloud (no browser required after the one-time setup).
  Enable with USE_OAUTH2=true.

Strategy C — Passive fallback
  If neither A nor B works, keeps the existing youtube.txt on disk.
  Re-checks every REFRESH_INTERVAL_HOURS and upgrades automatically
  the moment A or B becomes available.

REFRESH SCHEDULE
================
  • Startup: first check 30 seconds after server starts
  • Scheduled: every REFRESH_INTERVAL_HOURS (default 6 h)
  • On-demand: immediately when force_refresh() is called (bot-detection error)
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

# ── Configuration ─────────────────────────────────────────────────────────────

REFRESH_INTERVAL_HOURS = float(os.environ.get("COOKIE_REFRESH_HOURS", "6"))
STALE_AFTER_HOURS      = float(os.environ.get("COOKIE_STALE_HOURS", "12"))
COOKIE_BROWSER         = os.environ.get("COOKIE_BROWSER", "chrome").lower()
COOKIE_BROWSER_PROFILE = os.environ.get("COOKIE_BROWSER_PROFILE", "")
USE_OAUTH2             = os.environ.get("USE_OAUTH2", "").lower() in ("1", "true", "yes")

# The cookie filename that main.py's DOMAIN_COOKIE_MAP already expects.
YOUTUBE_COOKIE_FILE = "youtube.txt"


# ── Meta file helpers (compatible with main.py's _load_cookie_meta format) ───

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
    """Returns how old the current youtube.txt is in hours. None if unknown."""
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
    # Fall back to file mtime
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


# ── Strategy A: extract cookies from local browser ────────────────────────────

def _try_browser_extraction(cookies_dir: Path) -> bool:
    """
    Uses yt-dlp --cookies-from-browser to extract a fresh Netscape cookies.txt.
    Works on any machine with a browser. Returns True on success.
    """
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
        "https://www.youtube.com/",
    ]

    logger.info(f"[Strategy A] Extracting cookies from {COOKIE_BROWSER}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if tmp.exists() and tmp.stat().st_size > 200:
            shutil.move(str(tmp), str(dest))
            size = dest.stat().st_size
            _update_meta(cookies_dir, f"browser:{COOKIE_BROWSER}", size)
            logger.info(f"[Strategy A] ✓ Cookies refreshed from {COOKIE_BROWSER} ({size} bytes)")
            return True
        stderr = result.stderr.strip()
        logger.warning(f"[Strategy A] No usable cookies from browser. stderr: {stderr[:300]}")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
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


# ── Strategy B: OAuth2 device-code flow ───────────────────────────────────────

def _try_oauth2_extraction(cookies_dir: Path) -> bool:
    """
    Uses yt-dlp's built-in OAuth2 device-code flow.

    First run: yt-dlp prints a Google authorization URL to stdout/stderr.
    Open it in any browser once and authorize. The token is saved automatically
    and all future refreshes are silent.

    On Render: check the service logs right after first deploy for the URL.

    Returns True if a usable cookie file was produced.
    """
    dest = cookies_dir / YOUTUBE_COOKIE_FILE
    tmp  = cookies_dir / "_youtube_oauth2_tmp.txt"

    cmd = [
        "yt-dlp",
        "--username", "oauth2",
        "--password", "",
        "--cookies", str(tmp),
        "--skip-download",
        # Don't suppress output — the auth URL needs to be visible in logs.
        "https://www.youtube.com/",
    ]

    logger.info(
        "[Strategy B] Attempting OAuth2 device-code flow. "
        "If this is the first run, check logs for a Google authorization URL."
    )
    try:
        # Generous timeout: OAuth2 device-code expires in ~5 minutes. We give
        # 6 minutes so the user has time to see the URL and authorize before
        # yt-dlp times out internally.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=360)

        # Surface the auth URL even from quiet mode
        if result.stdout.strip():
            logger.info(f"[Strategy B] yt-dlp stdout: {result.stdout.strip()[:600]}")
        if result.stderr.strip():
            logger.info(f"[Strategy B] yt-dlp stderr: {result.stderr.strip()[:600]}")

        if tmp.exists() and tmp.stat().st_size > 200:
            shutil.move(str(tmp), str(dest))
            size = dest.stat().st_size
            _update_meta(cookies_dir, "oauth2", size)
            logger.info(f"[Strategy B] ✓ OAuth2 cookies written ({size} bytes)")
            return True

        logger.warning("[Strategy B] OAuth2 produced no usable cookie file.")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False

    except subprocess.TimeoutExpired:
        logger.warning(
            "[Strategy B] OAuth2 timed out. The device-code may have expired. "
            "Check earlier logs for the authorization URL and try force-refreshing."
        )
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False
    except FileNotFoundError:
        logger.warning("[Strategy B] yt-dlp not found in PATH.")
        return False
    except Exception as e:
        logger.warning(f"[Strategy B] Unexpected error: {e}")
        return False


# ── Main refresh logic ─────────────────────────────────────────────────────────

def refresh_cookies(cookies_dir: Path, force: bool = False) -> bool:
    """
    Attempt to refresh the YouTube cookies file using the best available strategy.

    Args:
        cookies_dir: Path where youtube.txt is stored (your DOWNLOADS_DIR).
        force:       If True, refresh even if cookies appear fresh.

    Returns True if cookies were successfully refreshed (or were already fresh).
    """
    age = _cookie_age_hours(cookies_dir)

    if not force:
        if age is not None and age < STALE_AFTER_HOURS:
            logger.debug(f"Cookies are {age:.1f}h old (< {STALE_AFTER_HOURS}h). Skipping.")
            return True  # Already fresh enough

    age_str = f"{age:.1f}h" if age is not None else "unknown age"
    logger.info(f"Refreshing YouTube cookies (age={age_str}, force={force})...")

    # Strategy A: local browser (best quality, local/VPS only).
    # Skipped when USE_OAUTH2=true so cloud deployments go straight to B.
    if not USE_OAUTH2:
        if _try_browser_extraction(cookies_dir):
            return True

    # Strategy B: OAuth2 device-code flow (headless, cloud-friendly).
    if _try_oauth2_extraction(cookies_dir):
        return True

    # All strategies failed — keep whatever is on disk.
    cookie_path = cookies_dir / YOUTUBE_COOKIE_FILE
    if cookie_path.exists():
        logger.warning(
            "All auto-refresh strategies failed. Keeping existing cookie file. "
            "Consider uploading a fresh one via /api/cookies/upload."
        )
    else:
        logger.error(
            "All auto-refresh strategies failed and no cookie file exists. "
            "YouTube bot-detection errors are likely. "
            "Please upload cookies via /api/cookies/upload or set USE_OAUTH2=true "
            "and check logs for the authorization URL."
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

    Then in your bot-detection error handler:
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
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Cookie refresher started. "
            f"interval={REFRESH_INTERVAL_HOURS}h, "
            f"stale_threshold={STALE_AFTER_HOURS}h, "
            f"strategy={'oauth2' if USE_OAUTH2 else f'browser:{COOKIE_BROWSER}'}"
        )

    def stop(self):
        """Stop the background thread gracefully."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def force_refresh(self):
        """
        Trigger an immediate cookie refresh in the background.
        Call this when a download fails with a bot-detection error.
        The refresh is async — the current job fails (unavoidable), but
        a retry ~30s later will pick up the fresh cookies.
        """
        logger.info("Bot-detection error — triggering immediate cookie refresh.")
        self._force_refresh_event.set()

    def _loop(self):
        # Let the server finish starting up, then do the first check.
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

            # Sleep until next scheduled interval; wake early on force_refresh.
            self._force_refresh_event.wait(timeout=REFRESH_INTERVAL_HOURS * 3600)

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