# FINAL DEPLOYMENT — RENDER ONLY

## 1. GitHub repository
Upload the contents of this package to the root of one final GitHub repository.

Do not create a nested `CinemaHUB-OG-FINAL/...` directory inside the repository. The repository root must directly contain `bot.py`, `requirements.txt`, and `stream_server/`.

Do not commit:
- `__pycache__/`
- `.pytest_cache/`
- `*.pyc`
- `.env`
- Telegram session files

The included `.gitignore` already excludes these generated/secret files.

## 2. Main Bot — Render

Create/use the main bot Web Service:

- Runtime: Python 3
- Branch: `main`
- Root Directory: blank
- Build Command: `python -m pip install -r requirements.txt`
- Start Command: `python bot.py`
- Health Check: `/health`

Suggested compute for testing: the Render Free Web Service. The bot is designed not to block HTTP health startup while MongoDB/Telegram workers retry in the background.

## 3. Main Bot environment

Keep the working values already used by the current bot. The following are the important settings:

```text
BOT_TOKEN=<main bot token>
API_ID=<telegram api id>
API_HASH=<telegram api hash>
SESSION_STRING=<main bot Telethon StringSession; required for /reindex and /reconcile>
MONGO_URI=<same mongo uri used by stream server>
DB_NAME=<same database name used by stream server>
DATABASE_CHANNEL_ID=<movie database channel id>
FSUB_CHANNELS=<comma-separated channels>
FSUB_INVITE_LINKS=<matching invite links>
SOFTURL_API=<softurl api token>
SOFTURL_BASE_URL=https://softurl.in/api
REQUIRE_FSUB=true
REQUIRE_SHORTLINK=true
SHORTLINK_TTL_SECONDS=1800
DELETE_AFTER_SECONDS=300
SEARCH_RESULT_DELETE_AFTER_SECONDS=600
INDEX_ON_START=false
AUTO_INDEX_NEW_POSTS=true
SEARCH_PAGE_SIZE=8
SEARCH_DB_CONCURRENCY=8
INDEX_BATCH_SIZE=250
CAPTION_WORKER_INTERVAL_SECONDS=2
CAPTION_EDIT_MIN_INTERVAL_SECONDS=1.1
ADMIN_IDS=<numeric telegram user ids>
PAYMENT_BOT_TOKEN=<payment bot token>
PAYMENT_BOT_USERNAME=visionaryowner_bot
PAYMENT_BOT_FORCE_POLLING=true
PREMIUM_MEMBER_TAG=PREMIUM
PREMIUM_QR_URL=https://t.me/+7_i3pMzJBTFlZTQ1
STREAM_BASE_URL=<streaming render service url>
STREAM_SIGNING_SECRET=<exactly the same secret used by streaming service>
STREAM_TOKEN_TTL_SECONDS=1800
STREAM_POWERED_BY=Cinema HUB OG
STREAM_SERVICE_BY=The Visionary Team
STREAM_CACHE_MAX_AGE_SECONDS=21600
```

Do not paste secrets into GitHub.

## 4. Streaming Server — Render Docker Web Service

Use the same GitHub repository and branch.

- Runtime: Docker
- Branch: `main`
- Root Directory: `stream_server`
- Dockerfile Path: `Dockerfile`
- Docker Context: `.`
- Health Check: `/health`

Environment:

```text
MONGO_URI=<same mongo uri as main bot>
DB_NAME=<same database name as main bot>
API_ID=<telegram api id>
API_HASH=<telegram api hash>
STREAM_SESSION_STRING=<valid Telethon StringSession>
STREAM_DATABASE_CHANNEL_ID=<same movie database channel>
STREAM_SIGNING_SECRET=<exactly same secret as main bot>
STREAM_POWERED_BY=Cinema HUB OG
STREAM_SERVICE_BY=The Visionary Team
STREAM_LOGO_URL=<optional public logo URL; leave blank to try Telegram channel profile photo>
STREAM_CACHE_MAX_AGE_SECONDS=21600
STREAM_TRANSCODE_CONCURRENCY=1
STREAM_MEDIA_CONCURRENCY=6
STREAM_CHUNK_SIZE=1048576
HLS_SEGMENT_SECONDS=6
```

Do not regenerate `STREAM_SIGNING_SECRET` between the two services.

## 5. Deployment order

1. Commit the final repository to GitHub `main`.
2. Deploy the streaming service on Render and wait for `/health` to report a configured service with FFmpeg available. The Telegram field may remain false until the session has connected; the first signed media request will retry the connection.
3. Copy the streaming Render URL.
4. Put that exact URL into the main bot's `STREAM_BASE_URL`.
5. Save/redeploy the main bot.
6. Create a fresh Premium Stream & Download action from Telegram. Do not reuse an old button URL.

## 6. Testing checklist

### Main bot
- `/start`
- F-Sub for Free and Premium
- Request Group search
- cinematic search status
- typo/correction flow
- results/pagination
- 10-minute cleanup
- Softurl flow
- Free delivery
- Premium purchase screenshot
- APPROVE / REJECT
- Premium status/tag
- Premium private search
- direct Premium file delivery

### Streaming
- `/health`
- fresh Premium signed `/watch/<token>` URL
- direct browser-compatible MP4 playback
- Download
- VLC button / MX Player button / share-copy fallback
- MKV/HEVC compatibility mode
- HLS status changes from `preparing` to `ready`
- expired/invalid token rejection
- non-Premium rejection

## 7. Important limitation

The free Render plan is intentionally used for this project. Direct Range streaming is designed to be the fast path. HEVC/MKV compatibility conversion uses FFmpeg and is serialized to one job by default because free CPU is limited. The service reports an explicit preparation state rather than pretending the conversion is instantaneous.
