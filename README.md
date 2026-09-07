# Telegram Video Downloader Bot

This project runs a Telegram bot and a Telegram Mini App for downloading supported TikTok, Facebook, and YouTube media. The bot uses SQLite for application data and exposes a small Flask health server together with the Mini App routes.

## Requirements

Python 3.11 or newer is required. `ffmpeg` is recommended for audio extraction and YouTube format merging. Current yt-dlp YouTube extraction also uses Node.js 22 or newer for JavaScript challenge solving.

```bash
sudo apt-get update
sudo apt-get install -y python3-venv ffmpeg nodejs
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` or export the variables in the VPS service environment. The application requires `BOT_TOKEN` and `ADMIN_ID`; do not commit either value or any database/runtime files.

```bash
export BOT_TOKEN='your-token'
export ADMIN_ID='your-telegram-user-id'
export DOMAIN='your-domain.example'
export PORT=8080
export MAX_DOWNLOAD_MB=100
python main.py
```

## VPS deployment

Run the process under `systemd`, `supervisord`, or another process manager. Keep the SQLite database and `temp_media/` on persistent local storage, and configure a reverse proxy with HTTPS for the Mini App. The health endpoints are `/` and `/health`; they intentionally return only a generic status.

A simple systemd service should use an absolute `WorkingDirectory`, an environment file outside Git, and the virtualenv interpreter. Configure log rotation and back up `bot_data.db` privately rather than committing it.

## Tests and checks

Run syntax checks and the standard-library test suite before deployment:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

The downloader enforces a 100 MB default streamed download limit. Set `MAX_DOWNLOAD_MB` to a different positive value when the VPS and Telegram limits justify it. Temporary files older than one hour are cleaned at startup and Mini App initialization; a periodic external cleanup job is still recommended for long-running deployments.

## Data and security notes

Runtime databases, user lists, bans, logs, temporary media, archives, and local environment files are excluded by `.gitignore`. Telegram Mini App `initData` is HMAC-verified and rejected after 24 hours. Expensive Mini App endpoints apply a per-user in-memory rate limit, and SQLite connections use WAL mode with a busy timeout. The rate limiter is process-local; use a reverse proxy or shared store if deploying multiple bot instances.

Only download content that you are authorized to access and redistribute. Provider availability and terms can change independently of this project.
