# yt-dlp Console

A local web app that turns your yt-dlp cheat sheet into clickable buttons.
FastAPI backend + a single-page frontend, runs entirely on your machine.

```
┌─────────────┐      POST /api/jobs        ┌──────────────┐
│  Browser UI │ ───────────────────────▶   │   FastAPI    │
│ (index.html)│  ◀─────────────────────    │   backend    │
└─────────────┘    GET /api/jobs (poll)    └──────┬───────┘
                                                   │ background thread
                                                   ▼
                                            yt_dlp.YoutubeDL
                                            (calls ffmpeg too)
```

## 1. Setup (one-time)

You already have most of this from your cheat sheet. From the project folder:

```powershell
pip install -r requirements.txt
```

Make sure FFmpeg is installed (you already have this command):

```powershell
winget install "FFmpeg (Essentials Build)"
```

## 2. Run it

```powershell
cd ytdlp-gui
uvicorn app.main:app --reload --port 8000
```

Then open **http://127.0.0.1:8000** in your browser. Leave the terminal
window open — that's your server. Closing it stops the app.

## 3. Using it

The left rail has one button per mode from your cheat sheet:

| Mode | What it does |
|---|---|
| **Video (MP4)** | Section 3 — pick a resolution or take best available |
| **Music (MP3)** | Section 2 — best audio, cover art, tags |
| **Playlist** | Section 4 — full playlist or a numbered range |
| **Link list (.txt)** | Paste or load a `.txt` of direct links (any mix of sites) — each line becomes its own tracked job, MP3 or MP4 for the whole batch |
| **Smart search (.txt)** | Section 6 — paste *search terms* (not links), one per line; it searches + filters out lyrics/visualizer junk automatically |
| **Facebook** | Section 7 — spoofed user-agent |
| **X / Twitter** | Section 8 — needs a `cookies.txt` path |
| **Any other site** | Everything else yt-dlp supports — Instagram, TikTok, SoundCloud, Vimeo, Reddit, etc. Paste the link, it just works for most sites without any special options |

**Link list vs. Smart search** — these look similar but do different things:
- **Link list** takes actual URLs and downloads each one directly. Use this for your `songs.txt`-style files that already contain links.
- **Smart search** takes plain text like `Artist - Song Title` and *searches* YouTube for it, filtering out lyric videos/visualizers/karaoke versions before grabbing the best match. Don't paste links into this one — it'll search for the URL text itself instead of recognizing it as a link.

Paste a link, adjust options if you want, click **Start download**. The job
appears in the queue on the right with a live progress bar — you can keep
queuing more downloads while one is running, since each runs in its own
background thread.

Downloaded files land in `app/downloads/`, optionally inside a subfolder you
name in the form (this maps to your `-o "Folder/%(title)s.%(ext)s"` pattern).

**Managing the queue:** finished jobs (done/error/cancelled) pile up over
time — click **Clear finished** in the queue header to wipe them out.
Running and queued jobs are never touched by this, so it's safe to click
mid-download.

**Theme:** click **Settings** in the bottom of the left rail to pick a
theme. **System** follows your OS's light/dark setting automatically and
updates live if you change it. There are also 5 dark/low-light variants
(Console Amber, True Black, Warm Dim, Slate, Forest Dim) and 2 light themes
(Paper, Daylight) if you want to pick one explicitly instead of following
the OS. Your choice is remembered across restarts (stored in the browser,
not the server).

## 4. The API (since you're learning FastAPI)

This app **is** a FastAPI REST API under the hood — the buttons just call it.
Worth poking at directly while you're learning:

- Interactive docs: **http://127.0.0.1:8000/docs** (auto-generated Swagger UI)
- `POST /api/jobs` — start a download. Body is a JSON version of all the
  options in the form (see `DownloadRequest` in `app/main.py`).
- `POST /api/jobs/batch` — Link list mode: body has a multi-line `urls`
  field, fans out into one job per line.
- `GET /api/jobs` — list every job and its current status/progress.
- `GET /api/jobs/{id}` — poll a single job (this is what the UI calls every
  ~1.2s to animate the progress bar).
- `PUT /api/jobs/{id}/retry` — re-run a failed job with the same options.
- `DELETE /api/jobs/{id}` — cancel a running job, or clear a finished one
  from the list.
- `DELETE /api/jobs/clear-finished` — bulk-remove every done/error/cancelled
  job at once, leaving running/queued jobs alone.

Try it from `/docs`: expand `POST /api/jobs`, click "Try it out", paste a
YouTube URL with `"mode": "mp4"`, hit Execute — same thing the button does.

## 5. Notes on what's simplified vs. your raw commands

- **Multiple `.txt` files at once** (your PowerShell `Get-ChildItem` loop,
  section 12): the "Smart batch" mode covers the single-file case well. For
  running several list files into separate folders unattended, your original
  PowerShell loop is still the more direct tool — this app is built for
  one-off/interactive use, not unattended batch jobs. Worth adding as a v2
  feature if you want it (an endpoint that accepts multiple lists + folder
  names).
- **`--download-archive`** is on by default ("Skip already-downloaded"
  toggle) and is stored per-subfolder, so different projects don't share one
  archive file.
- **Raw flags field** (Advanced section): yt-dlp's Python API takes a
  structured options dict, not a flag string, so arbitrary CLI flags aren't
  auto-applied — the field just logs your note as a reminder. If you hit a
  site that needs a flag not covered by the form, tell me which one and I'll
  wire it in properly (most are a 2-line addition to `build_ydl_opts` in
  `app/main.py`).
- **Job history** is in-memory only — restarting the server clears the queue
  list (downloaded files themselves are obviously untouched). Say the word
  if you want it to persist across restarts; it's a small SQLite addition.

## 6. Project structure

```
ytdlp-gui/
├── requirements.txt
├── app/
│   ├── main.py          ← FastAPI backend, all routes + yt-dlp logic
│   ├── static/
│   │   └── index.html   ← the whole frontend (HTML/CSS/JS, no build step)
│   └── downloads/        ← files land here (+ subfolders you name)
```



yt-dlp Console

A local web app that turns your yt-dlp cheat sheet into clickable buttons.
FastAPI backend + a single-page frontend, runs entirely on your machine.

1. Setup (one-time)

You already have most of this from your cheat sheet. From the project folder, run:

pip install -r requirements.txt


(Note: I added pyzipper to your requirements to handle the AES-256 encrypted zips natively without needing 7-Zip installed)

Make sure FFmpeg is installed (you likely already have this):

winget install "FFmpeg (Essentials Build)"


2. Run it & Connect with iPhone

Option A: Just using your computer

Open PowerShell in your project folder and run:

cd ytdlp-gui
uvicorn app.main:app --reload --port 8000


Then open http://127.0.0.1:8000 in your web browser.

Option B: Connect via iPhone (or any device on your Wi-Fi)

By default, the server only listens to your local machine (127.0.0.1). To tell the server to listen to your entire home network so your iPhone can connect to it, you need to use the --host 0.0.0.0 flag:

Find your computer's IP address: Open a new PowerShell window and type ipconfig. Look for the "IPv4 Address" under your active Wi-Fi or Ethernet adapter (it will look something like 192.168.1.15).

Start the server:
cd ytdlp-gui
uvicorn app.main:app --host 0.0.0.0 --port 8000

Open Safari on your iPhone: Type your computer's IP address followed by the port: http://192.168.1.15:8000.

As long as your computer and iPhone are on the same Wi-Fi network, you can drop links directly from your phone into the UI, and the heavy lifting (and file storage) will happen safely on your computer.

3. New Features Included

Network Resilience: The backend natively uses "infinite retries" with yt-dlp. If you disconnect your Wi-Fi mid-download, it won't crash or trigger an error. It will safely pause, continuously check the connection, and immediately resume downloading exactly where it left off the second your network is restored.

Enhanced Download Stats: The UI tracks and displays the downloaded file size versus the total file size (e.g., 15.0MiB / 50.0MiB), alongside your active download speed.

Encrypted Batch Zipping: When using the "Link list" mode, you can check a box to create a ZIP file and assign a password. The system will download all the videos, display a new "Zip task" in your queue, and securely pack them using pyzipper (AES-256 encryption) to keep private videos safe.

4. Using it

The left rail has one button per mode from your cheat sheet:

Mode

What it does

Video (MP4)

Section 3 — pick a resolution or take best available

Music (MP3)

Section 2 — best audio, cover art, tags

Playlist

Section 4 — full playlist or a numbered range

Link list (.txt)

Paste or load a .txt of direct links. Supports automatic encrypted zipping!

Smart search (.txt)

Section 6 — paste search terms, one per line; skips lyrics/visualizer junk automatically

Facebook

Section 7 — spoofed user-agent

X / Twitter

Section 8 — needs a cookies.txt path

Any other site

Everything else yt-dlp supports. Paste the link, it just works for most sites.