# Setup & Deploy Guide — yt-dlp Console (DB + Storage + Login version)

This version adds three things on top of the original local-only app:

1. **Postgres (Neon)** — job history now survives restarts/redeploys.
2. **Cloudflare R2** — finished files are uploaded to cloud storage instead
   of sitting on Render's disk (which wipes itself on every restart).
3. **Shared passphrase login** — one password, shared with your friends,
   gates the whole app. Not individual accounts — just a single locked door.

## 1. What changed (file-by-file)

- **`app/main.py`** — rewritten. Jobs are now persisted to Postgres at each
  status change. After a successful download, the finished file is
  uploaded to R2 and the local copy is deleted. Every route except
  `/login`, `/api/login`, `/api/logout`, `/api/auth/status`, `/api/health`,
  and static assets now requires a valid login cookie.
- **`app/database.py`** *(new)* — SQLAlchemy models + connection setup.
- **`app/storage.py`** *(new)* — R2 upload/download helpers via boto3.
- **`app/auth.py`** *(new)* — passphrase check + signed session cookies.
- **`app/static/login.html`** *(new)* — the login page.
- **`requirements.txt`** — added `sqlalchemy`, `psycopg2-binary`, `boto3`,
  `itsdangerous`, `python-dotenv`.
- **`.env.example`** *(new)* — template for required environment variables.

## 2. Local setup

```powershell
cd ytdlp-gui
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` in the project root (same folder as
`requirements.txt`), and fill in your real values:

- `DATABASE_URL` — from Neon's "Connect" button
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT_URL` — from Cloudflare R2
- `R2_BUCKET_NAME` — should be `ytdlp-files` if you followed the setup steps
- `APP_PASSPHRASE` — pick a password to share with friends
- `SECRET_KEY` — generate with:
  ```powershell
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

Then run as usual:

```powershell
uvicorn app.main:app --reload --port 8000
```

Visit `http://127.0.0.1:8000` — you should land on the login page. Enter
your `APP_PASSPHRASE` value and you should reach the dashboard.

## 3. Deploying on Render

1. Push this code to a GitHub repo (if it isn't already there).
2. In Render: **New → Web Service**, connect the repo.
3. **Build Command:** `pip install -r requirements.txt`
4. **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   (no `--reload` in production)
5. Under **Environment**, add every variable from `.env.example` with your
   real values — same names, same values you used locally.
6. Deploy. Render gives you a public HTTPS URL (e.g.
   `https://ytdlp-gui-xxxx.onrender.com`).
7. Share that URL + your `APP_PASSPHRASE` with your friends. Nothing else
   needed on their end — no signup, no account.

## 4. About YouTube's bot-check ("Sign in to confirm you're not a bot")

This is unrelated to the login system we just built — it's YouTube
challenging your **server's IP address**, not your app's users. If it
shows up:

- It happens for everyone using the app at once, since it's tied to your
  Render instance's outbound IP, not each visitor.
- The fix: export a `cookies.txt` from your own YouTube-logged-in browser
  session (there are browser extensions for this, e.g. "Get cookies.txt"),
  then upload it to Render as a **Secret File** (Render's dashboard has
  this under the service's "Environment" tab) and point the existing
  `cookies_file` option at its path.
- This is maintenance only you do — friends never see or interact with it.
- Cookies can go stale after a while; if downloads start failing with a
  bot-check error again, re-export and re-upload.

## 5. Known limitations carried over from the original app

- **Multiple `.txt` batch files at once** — still a v2 feature, same as
  before, unrelated to this upgrade.
- **Raw flags field** — still just a logged note, not auto-applied.
- The shared passphrase is **one password for everyone** — if you want
  individual accounts/permissions later, that's a bigger follow-up
  (would mean adding a `users` table and a real signup/login flow).

## 6. Quick sanity checklist after deploying

- [ ] Visiting the Render URL redirects to `/login`
- [ ] Wrong passphrase shows an error, doesn't let you in
- [ ] Correct passphrase logs you in and shows the dashboard
- [ ] Starting a download shows progress as before
- [ ] After it finishes, a download link/button works and gives you the file
- [ ] Restarting the Render service (Manual Deploy → Clear cache & deploy)
      still shows old job history afterward — proves the DB persistence
      is actually working
