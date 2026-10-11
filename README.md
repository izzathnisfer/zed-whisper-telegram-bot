# Private Groq-Powered Telegram Transcription Bot

A fast, private Telegram bot that transcribes voice notes, audio, and video using
Groq's `whisper-large-v3-turbo` model.

## Features

- Owner-only access through `ALLOWED_USER_ID`
- Normal copyable Telegram replies for media up to five minutes
- Ordered five-minute chunks and a merged `.txt` result for longer media
- Live progress updates while long recordings are prepared and transcribed
- Two recordings can transcribe concurrently while commands remain responsive
- Persistent local tracking of all four Groq free-plan limits
- Atomic rate-limit accounting across parallel jobs and explicit retries
- Automatic short pauses for the 20-requests-per-minute limit
- A two-hour maximum for one transcription job
- Incoming cache files are unlinked immediately after the private working copy
  is made; working audio and generated chunks are deleted on success or failure

## Commands

- `/start` — show the welcome message
- `/usage` — show the primary two-hours-per-rolling-hour audio meter
- `/usage_detailed` — show audio/hour, audio/day, requests/minute, and
  requests/day with progress bars and remaining capacity
- `/language [code|auto]` — view or set transcription language (e.g. `/language ta` for Tamil, `/language auto` for auto-detect)
- `/version` — show current bot version (`v2.0.1`) and release changelog

Usage is stored in `data/groq_usage.sqlite3` by default and survives bot
restarts. Tracking begins when this version first starts; it cannot reconstruct
requests made before installation or requests made outside this bot.

## Groq free-plan limits used by the bot

| Limit | Allowance | Five-minute chunks |
| --- | ---: | ---: |
| Requests/minute | 20 | 20 |
| Requests/day | 2,000 | 2,000 |
| Audio/hour | 7,200 seconds (2 hours) | 24 |
| Audio/day | 28,800 seconds (8 hours) | 96 |

The audio/hour allowance is treated as the main limit. All counters use
conservative rolling windows and update automatically as recorded attempts age
out. Audio shorter than ten seconds is counted as ten seconds.

## Requirements

- Ubuntu or Debian Linux
- Python 3.9+
- FFmpeg and FFprobe
- Telegram bot token
- Groq API key
- Your Telegram user ID

## Install

```bash
git clone https://github.com/izzathnisfer/zed-whisper-telegram-bot.git
cd zed-whisper-telegram-bot
chmod +x setup.sh
./setup.sh
```

Copy the environment template, add the three private values, then start the
service:

```bash
cp .env.example .env
nano .env
sudo systemctl start zed-whisper-bot
sudo systemctl status zed-whisper-bot
```

Never commit `.env`; it is ignored by Git.

## Telegram file-size note

Telegram's hosted Bot API only permits bots to download files up to 20 MB.
Long audio that stays under 20 MB can be chunked normally. For larger files,
run a separate Local Telegram Bot API server and configure:

```dotenv
TELEGRAM_LOCAL_MODE=true
TELEGRAM_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
TELEGRAM_LOCAL_DATA_DIR=/var/lib/telegram-bot-api
```

The Local Bot API server requires a private Telegram `API_ID` and `API_HASH`.
Keep them in the server's private environment, never in this repository.

Production deployment templates are provided in `deploy/`. The container image
is pinned by immutable digest, listens only on `127.0.0.1:8081`, and shares a
private ACL-protected data directory with the Python bot. A root-owned cleanup
helper receives the sensitive cache path only through stdin, validates that it
resolves inside that directory, refuses state/database files, and unlinks the
exact incoming media file. Install `docker.io` and `acl`, then run
`deploy/install-local-bot-api.sh` with `sudo`. Telegram requires calling the
cloud Bot API `logOut` method before running
`deploy/enable-local-bot-api-for-bot.sh` to complete the cutover.

## Maintenance

```bash
journalctl -u zed-whisper-bot -f
sudo systemctl restart zed-whisper-bot
```
