"""
storage.py
----------
Wraps boto3 to talk to Cloudflare R2 (S3-compatible). Handles:
  - uploading a finished download from local disk into the bucket
  - generating short-lived pre-signed URLs so friends can fetch the file
    directly from R2 (fast, and R2 has zero egress fees so this never
    costs you anything extra)
  - deleting the local copy after a successful upload, since Render's
    disk is ephemeral and shouldn't be relied on for storage

All four R2 credentials come from environment variables — never hardcode
them. See .env.example for the exact names expected.

FIX — R2 SSL / upload failures
-------------------------------
The original code let upload errors bubble up and immediately mark the
job ERROR.  That's too harsh: transient SSL handshakes, connection resets
from Cloudflare's edge, and boto3's "An error occurred (ConnectionError)"
all look like hard failures but disappear on retry.

Changes:
  - upload_file() now retries up to MAX_UPLOAD_RETRIES times with
    exponential back-off before giving up.
  - A new exception class UploadFailedError carries the last underlying
    exception so callers can surface a clean message.
  - get_presigned_download_url() and delete_from_bucket() are unchanged
    in behaviour; their existing bare except guards are left in place.

FIX — Pylance "reportMissingImports" warnings
----------------------------------------------
The # pyright: ignore comments on the boto3 imports are correct — they
suppress the static-analysis noise that appears when boto3/botocore are
installed in the project venv but Pylance can't find their type stubs.
This is a known limitation of boto3 (it ships no py.typed marker).
Nothing needs to change in the code; the fix is to add a pyrightconfig.json
to the project root (see note at the bottom of this file) so Pylance
resolves the venv correctly and stops flagging the imports.
"""

import os
import time
import mimetypes

import boto3   # pyright: ignore[reportMissingImports]
from botocore.client import Config  # pyright: ignore[reportMissingImports]
from botocore.exceptions import BotoCoreError, ClientError  # pyright: ignore[reportMissingImports]
from dotenv import load_dotenv

load_dotenv()

R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME   = os.environ.get("R2_BUCKET_NAME", "ytdlp-files")
R2_ENDPOINT_URL  = os.environ.get("R2_ENDPOINT_URL")   # https://<account_id>.r2.cloudflarestorage.com

_REQUIRED = {
    "R2_ACCOUNT_ID":        R2_ACCOUNT_ID,
    "R2_ACCESS_KEY_ID":     R2_ACCESS_KEY_ID,
    "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
    "R2_ENDPOINT_URL":      R2_ENDPOINT_URL,
}
_missing = [k for k, v in _REQUIRED.items() if not v]
if _missing:
    raise RuntimeError(
        f"Missing R2 environment variables: {', '.join(_missing)}. "
        "Set them locally in .env, or on Render under the service's Environment tab."
    )

_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(
        signature_version="s3v4",
        # Increase connect/read timeouts — large video files on a slow
        # Render instance can saturate the socket before the default 60 s
        # read timeout fires, which was causing spurious SSL resets.
        connect_timeout=15,
        read_timeout=120,
        retries={"max_attempts": 0},   # we handle retries ourselves
    ),
    region_name="auto",  # R2 doesn't use AWS regions, but boto3 wants something
)

# How many times to retry a failed upload before giving up.
MAX_UPLOAD_RETRIES = 4
# Base delay (seconds) for exponential back-off: 2, 4, 8, 16 …
_RETRY_BASE = 2


class UploadFailedError(Exception):
    """Raised when upload_file() exhausts all retries."""


def upload_file(local_path: str, job_id: str, filename: str) -> str:
    """
    Uploads a finished file from local disk to R2.
    Returns the storage key (path inside the bucket) to save on the job record.

    The storage key intentionally does NOT embed the raw filename. Video/post
    titles can contain emoji, pipes, colons, and other characters that some
    HTTP/SSL stacks mishandle when they end up as part of a request path —
    this caused real upload failures (SSL handshake errors) on titles with
    heavy emoji use. The key is built from the job_id (already a safe, unique
    hex string) plus just the file extension. The original filename is
    preserved separately (job.filename) and reapplied via Content-Disposition
    when the file is downloaded, so the person still gets a sensible filename
    when they save it.

    Retries up to MAX_UPLOAD_RETRIES times with exponential back-off.
    Raises UploadFailedError if all attempts fail.
    """
    ext = os.path.splitext(filename)[1]   # e.g. ".mp4" — empty string if none
    key = f"jobs/{job_id}/file{ext}"
    content_type, _ = mimetypes.guess_type(filename)
    extra_args: dict = {"ContentType": content_type} if content_type else {}

    last_exc: Exception = RuntimeError("No upload attempted")
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            _client.upload_file(local_path, R2_BUCKET_NAME, key, ExtraArgs=extra_args)
            return key  # success
        except (BotoCoreError, ClientError, OSError) as exc:
            last_exc = exc
            if attempt < MAX_UPLOAD_RETRIES:
                wait = _RETRY_BASE ** attempt
                time.sleep(wait)
            # else: fall through to raise below

    raise UploadFailedError(
        f"Upload failed after {MAX_UPLOAD_RETRIES} attempts: {last_exc}"
    ) from last_exc


def delete_local_file(local_path: str) -> None:
    """Removes the local copy after a successful upload (Render's disk is ephemeral anyway)."""
    try:
        if os.path.exists(local_path):
            os.remove(local_path)
    except OSError:
        pass  # not fatal — worst case, an orphaned file sits on ephemeral disk until next restart


def get_presigned_download_url(
    storage_key: str,
    filename: str | None = None,
    expires_in: int = 3600,
) -> str:
    """
    Generates a temporary, expiring URL that lets someone download the file
    directly from R2 without needing R2 credentials themselves.
    Default expiry: 1 hour.

    Setting ResponseContentDisposition makes the browser save the file
    (Chrome's download prompt / straight into Downloads) instead of just
    opening it inline in a new tab, which is what happens by default for
    browser-renderable types like video/mp4 or audio/m4a.
    """
    params: dict = {"Bucket": R2_BUCKET_NAME, "Key": storage_key}
    if filename:
        params["ResponseContentDisposition"] = (
            f'attachment; filename="{_safe_header_filename(filename)}"'
        )

    return _client.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def _safe_header_filename(filename: str) -> str:
    """
    Strips characters that could break or inject into the Content-Disposition
    header (quotes, control characters, newlines) and drops non-ASCII so
    emoji-heavy video titles can't cause malformed/rejected headers. This
    only affects the filename shown when saving — the actual file content
    is untouched.
    """
    # Drop anything outside printable ASCII, then strip quote/backslash
    # characters which could otherwise break out of the quoted value.
    cleaned = filename.encode("ascii", "ignore").decode("ascii")
    cleaned = (
        cleaned
        .replace('"', "")
        .replace("\\", "")
        .replace("\n", "")
        .replace("\r", "")
        .strip()
    )
    return cleaned or "download"


def delete_from_bucket(storage_key: str) -> None:
    """Removes a file from R2 entirely (used when a job is deleted/cleared)."""
    try:
        _client.delete_object(Bucket=R2_BUCKET_NAME, Key=storage_key)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HOW TO FIX THE PYLANCE "reportMissingImports" WARNINGS IN VS CODE
# ---------------------------------------------------------------------------
# Those two yellow warnings in the Problems panel:
#   Import "boto3" could not be resolved
#   Import "botocore.client" could not be resolved
# are NOT runtime errors — boto3 is installed and working fine.  They happen
# because Pylance can't find the active venv automatically.
#
# Fix (one-time, no code changes needed):
#
# 1. Make sure your venv is activated in the terminal and boto3 is installed:
#      pip install boto3
#
# 2. Tell VS Code which Python interpreter to use:
#    Ctrl/Cmd+Shift+P  →  "Python: Select Interpreter"
#    Choose the one inside your .venv / venv folder
#    (e.g.  ./.venv/bin/python  or  ./venv/Scripts/python.exe on Windows)
#
# 3. Optionally, add a pyrightconfig.json to the project root so it always
#    uses the right venv even without a VS Code workspace setting:
#
#    {
#      "venvPath": ".",
#      "venv": ".venv"
#    }
#
# After step 2 the warnings disappear immediately — no restart required.
# The  # pyright: ignore[reportMissingImports]  comments in this file are
# kept as a safety net for CI environments where stubs aren't installed.
