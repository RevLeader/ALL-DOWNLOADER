"""
auth.py
-------
Simple shared-passphrase gate. Not per-user accounts — everyone you share
the app with uses the same passphrase, set once via the APP_PASSPHRASE
environment variable.

How it works:
  1. POST /login with the passphrase -> if correct, we set a signed cookie
     ("session") containing a marker that says "this browser is authed".
  2. The cookie is signed with itsdangerous using SECRET_KEY, so it can't
     be forged or edited by the client — but it isn't encrypted, so don't
     put sensitive data inside it (we don't; it just says "ok").
  3. A FastAPI dependency (require_login) checks for that cookie on every
     protected route and raises a redirect/401 if it's missing or invalid.
  4. Cookie expires after COOKIE_MAX_AGE seconds (default: 30 days), so
     friends don't have to log in every single visit.
"""

import os
import hmac

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
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
    return hmac.compare_digest(submitted.strip(), APP_PASSPHRASE.strip())


def create_session_token() -> str:
    return _serializer.dumps({"authed": True})


def verify_session_token(token: str) -> bool:
    try:
        data = _serializer.loads(token, max_age=COOKIE_MAX_AGE)
        return bool(data.get("authed"))
    except (BadSignature, SignatureExpired):
        return False


def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return verify_session_token(token)


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
