"""
auth.py
-------
Simple shared-passphrase gate, PLUS a private per-browser identity layered
on top. Still just one passphrase for everyone (low friction — no sign-up
forms), but each browser that logs in gets its own random, unguessable
user_id baked into its session cookie. Job history is scoped to that
user_id, so friends sharing the same passphrase don't see each other's
downloads — similar to how a free web tool (audio trimmer, converter, etc.)
gives every visitor their own private history with zero sign-up.

How it works:
  1. POST /login with the passphrase -> if correct, we set a signed cookie
     ("session") containing {"authed": True, "uid": "<random hex>"}.
     A fresh random uid is generated once per successful login.
  2. The cookie is signed with itsdangerous using SECRET_KEY, so it can't
     be forged or edited by the client — but it isn't encrypted, so don't
     put sensitive data inside it (we don't; uid is just an opaque id).
  3. A FastAPI dependency (require_login) checks for that cookie on every
     protected route and raises a redirect/401 if it's missing or invalid.
     get_user_id() pulls the uid back out for scoping queries.
  4. Cookie expires after COOKIE_MAX_AGE seconds (default: 30 days), so
     friends don't have to log in every single visit — and keep the SAME
     uid (and therefore the same job history) across that whole period,
     since the uid is only (re)generated at login time, not per request.

LIMITATION: this is per-browser, not a real account. Clearing cookies,
using a different browser, or switching to incognito means a "new" empty
identity with no way to recover the old one's history. That's an
intentional tradeoff for zero-friction entry — flagged so it's not a
surprise later.
"""

import os
import hmac
import secrets
from typing import Optional

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired  # pyright: ignore[reportMissingImports]
from dotenv import load_dotenv

load_dotenv()

APP_PASSPHRASE = os.environ.get("APP_PASSPHRASE")
SECRET_KEY = os.environ.get("SECRET_KEY")

if not APP_PASSPHRASE:
    raise RuntimeError(
        "APP_PASSPHRASE is not set. This is the shared password your friends "
        "will use to log in. Set it as an environment variable (locally in "
        ".env, on Render under the service's Environment tab)."
    )
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. This signs the login session cookie so it "
        "can't be forged. Set it to any long random string — e.g. generate "
        "one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

COOKIE_NAME = "session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days, in seconds

_serializer = URLSafeTimedSerializer(SECRET_KEY)


def check_passphrase(submitted: str) -> bool:
    """Constant-time comparison to avoid leaking timing info about the correct passphrase."""
    return hmac.compare_digest(submitted.strip(), APP_PASSPHRASE.strip())  # type: ignore[arg-type]


def create_session_token() -> str:
    """
    Called once, at successful login. Generates a fresh random user_id for
    this browser and bakes it into the signed cookie alongside the auth
    marker. token_hex(16) gives 128 bits of randomness — not guessable.
    """
    user_id = secrets.token_hex(16)
    return _serializer.dumps({"authed": True, "uid": user_id})


def _decode_session_token(token: str) -> Optional[dict]:
    try:
        return _serializer.loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def verify_session_token(token: str) -> bool:
    data = _decode_session_token(token)
    return bool(data and data.get("authed"))


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return verify_session_token(token)


def get_user_id(request: Request) -> str:
    """
    Returns this browser's private user_id, pulled from the signed session
    cookie. Use this to scope job queries/creation so people sharing the
    same passphrase don't see each other's downloads.

    Only call this on routes already behind require_login (or after
    confirming is_logged_in) — it raises if there's no valid session,
    since a missing uid should never silently fall through to showing
    someone the wrong (or no) job list.
    """
    token = request.cookies.get(COOKIE_NAME)
    data = _decode_session_token(token) if token else None
    if not data or not data.get("uid"):
        raise HTTPException(status_code=401, detail="Not logged in")
    return data["uid"]


def get_user_id_from_token(token: str) -> str:
    """
    Extracts the uid directly from a token string (not from a Request object).
    Used right after create_session_token() in the /api/login route, before
    the cookie has been set in the browser, to kick off background tasks that
    need the new uid (e.g. _claim_legacy_jobs).

    Raises ValueError if the token is invalid (should never happen since
    we just minted it, but defensive).
    """
    data = _decode_session_token(token)
    if not data or not data.get("uid"):
        raise ValueError("Invalid or expired token — cannot extract user_id")
    return data["uid"]


def require_login(request: Request):
    """
    FastAPI dependency for protected routes. Use as:
        @app.get("/api/jobs", dependencies=[Depends(require_login)])

    Raises 401 for API calls (so the frontend JS can detect it and redirect),
    rather than doing the redirect itself — keeps API and page routes
    behaving consistently as JSON-in, JSON-out.
    """
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Not logged in")


def require_login_page(request: Request):
    """
    Same check, but for full-page routes (like the dashboard itself) where
    a redirect to /login makes more sense than a raw 401 JSON blob.
    Use as a manual check inside the route, not as a Depends(), since it
    returns a response object directly:

        @app.get("/")
        def dashboard(request: Request):
            redirect = require_login_page(request)
            if redirect:
                return redirect
            return FileResponse(...)
    """
    if not is_logged_in(request):
        return RedirectResponse(url="/login")
    return None
