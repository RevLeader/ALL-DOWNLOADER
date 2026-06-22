# Automatic YouTube Cookie Refresh — Setup Guide

## The Problem

YouTube's bot detection triggers when yt-dlp makes requests without a valid, 
recent session cookie. The error looks like:
> "Sign in to confirm you're not a bot"

This happens more often as cookies age (typically after 7–14 days). The fix 
is keeping a fresh `youtube.txt` cookie file on disk at all times, automatically.

---

## Files Added

| File | Purpose |
|------|---------|
| `app/cookie_refresher.py` | Background service that refreshes cookies |
| `main_patch.py` | Shows exactly what to change in `main.py` |

---

## Step 1 — Install the file

Copy `cookie_refresher.py` into your `app/` directory (same folder as `main.py`):

```
your-project/
  app/
    main.py
    auth.py
    cookie_refresher.py   ← place here
    database.py
    storage.py
```

---

## Step 2 — Patch main.py (3 small changes)

### 2a. Import the refresher
Near the top of `main.py`, before the existing `from app.auth import` block:

```python
from app.cookie_refresher import CookieRefresher   # ADD THIS
```

### 2b. Start the refresher after DOWNLOADS_DIR is created (~line 51)

```python
APP_DIR = Path(__file__).parent
DOWNLOADS_DIR = APP_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ADD THESE TWO LINES:
cookie_refresher = CookieRefresher(cookies_dir=DOWNLOADS_DIR)
cookie_refresher.start()
```

### 2c. Trigger force-refresh on bot errors in `run_job()`

Find the `except yt_dlp.utils.DownloadError as e:` block inside `run_job()` 
and add the bot-detection check:

```python
    except yt_dlp.utils.DownloadError as e:
        if job.cancel_requested:
            job.status = JobStatus.CANCELLED
            job.add_log("Cancelled.")
        else:
            job.status = JobStatus.ERROR
            job.error = str(e)
            job.add_log(f"Error: {e}")

            # ADD THIS BLOCK:
            _BOT_PHRASES = ["sign in to confirm", "not a bot", "http error 429", "too many requests"]
            if any(p in str(e).lower() for p in _BOT_PHRASES):
                job.add_log("⚠ Bot check detected — refreshing cookies in background. Retry in ~30s.")
                cookie_refresher.force_refresh()
            # END ADD

        _persist(job)
```

### 2d. (Optional) Add status/control API endpoints

After your existing `/api/cookies/status` route, add:

```python
@app.get("/api/cookies/refresher", dependencies=[Depends(require_login)])
def cookie_refresher_status():
    return cookie_refresher.status()

@app.post("/api/cookies/force_refresh", dependencies=[Depends(require_login)])
def trigger_cookie_refresh():
    cookie_refresher.force_refresh()
    return {"ok": True, "message": "Refresh triggered. Poll /api/cookies/refresher for status."}
```

---

## Step 3 — Choose your refresh strategy

The refresher tries three strategies in order. Pick the one that fits your deployment.

---

### Strategy A — Browser extraction (best quality, local/VPS only)

**Works on:** Your local machine, a VPS where you can log into YouTube in a browser.  
**Does NOT work on:** Render free tier, Railway, Fly.io, or any platform without a GUI.

How it works: yt-dlp reads directly from Chrome/Firefox's cookie store.
Cookies are always current because they come from your actual logged-in browser.

**Setup:**
1. Log into YouTube in Chrome (or Firefox/Edge) on your server machine — just once.
2. Set the environment variable:
   ```
   COOKIE_BROWSER=chrome    # or: firefox, edge, safari
   ```
3. That's it. The refresher will pull fresh cookies from that browser every 6 hours.

If your browser profile isn't the default:
```
COOKIE_BROWSER_PROFILE=Profile 1
```

---

### Strategy B — OAuth2 (headless, cloud-friendly, one-time setup)

**Works on:** Render, Railway, Fly.io, any headless server.  
**Requires:** One-time browser authorization.

**Setup:**
```bash
pip install yt-dlp-get-pot
```

Set environment variable:
```
USE_OAUTH2=true
```

On first server start, the refresher will print a URL to your logs:
```
Please open this URL in your browser: https://accounts.google.com/o/oauth2/...
```

Open that URL once in any browser, authorize with your Google account, and 
the token is saved. Every refresh after that is fully automatic and silent.

**On Render:** Check the service logs for the URL right after first deploy.

---

### Strategy C — Automatic fallback

If neither A nor B is configured or works, the refresher:
- Keeps using whatever `youtube.txt` is already on disk
- Re-checks every 6 hours and upgrades automatically if a browser or OAuth2 becomes available
- Logs a clear warning so you know manual action is needed

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COOKIE_BROWSER` | `chrome` | Browser for Strategy A: `chrome`, `firefox`, `edge`, `safari` |
| `COOKIE_BROWSER_PROFILE` | *(default profile)* | Browser profile name (optional) |
| `COOKIE_REFRESH_HOURS` | `6` | How often to refresh (in hours) |
| `COOKIE_STALE_HOURS` | `12` | Age threshold before forced refresh |
| `USE_OAUTH2` | `false` | Set to `true` to use Strategy B |

---

## How the refresh schedule works

| Trigger | When |
|---------|------|
| Startup check | 30 seconds after server starts |
| Scheduled | Every `COOKIE_REFRESH_HOURS` (default: 6h) |
| On-demand | Immediately when a bot-detection error is caught |

The on-demand trigger is key: even if the scheduled refresh hasn't run yet,
a bot-detection failure in `run_job()` will kick off a background refresh 
immediately. The current job fails (unavoidable — the cookie was stale), but 
a retry of the same job 30 seconds later will succeed with fresh cookies.

---

## Verifying it's working

Check the refresher status via the API:
```
GET /api/cookies/refresher
```

Response:
```json
{
  "running": true,
  "last_refresh": "2025-08-10T14:23:01",
  "cookie_age_hours": 2.4,
  "cookie_file_exists": true,
  "cookie_size_bytes": 4821,
  "refresh_strategy": "browser:chrome",
  "auto_refreshed": true,
  "stale_after_hours": 12.0,
  "refresh_interval_hours": 6.0
}
```

Or check the existing `/api/cookies/status` — if `status` shows `"fresh"` and 
`age_days` stays low even without you manually uploading, the auto-refresh is working.

---

## Render-specific notes

- Strategy A (browser extraction) will **not** work on Render — there's no browser.
- Use Strategy B (OAuth2) with `USE_OAUTH2=true` in Render's Environment settings.
- After first deploy, watch the logs for the OAuth2 authorization URL. Open it once.
- After authorization, the token is saved to `DOWNLOADS_DIR` — it persists between 
  deploys as long as your Render disk is attached (or you use a persistent volume).
- If your Render service has no persistent disk, the OAuth token will be lost on 
  restart. In that case, also upload a cookie file via `/api/cookies/upload` as 
  a fallback — the refresher respects manually uploaded files and won't overwrite 
  them until they're older than `COOKIE_STALE_HOURS`.

---

## Manual cookie upload still works

The automatic refresher and the existing `/api/cookies/upload` endpoint 
coexist cleanly:

- If you upload a fresh cookie file manually, the refresher sees it as "fresh" 
  and won't touch it until it ages past `COOKIE_STALE_HOURS`.
- If the auto-refresh fails, your manually uploaded file stays in place as a 
  fallback until it expires.
- The refresher never deletes a cookie file — it only overwrites with a newer one.
