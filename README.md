# Cinema HUB OG — Final Render Build

This is the consolidated production baseline for the Cinema HUB OG Telegram bot and its Premium-only streaming server.

## Services

### 1) Main Bot — Render Web Service
- Root directory: repository root
- Runtime: Python 3
- Build: `python -m pip install -r requirements.txt`
- Start: `python bot.py`
- Health: `/health`

### 2) Streaming Server — Render Docker Web Service
- Root directory: `stream_server`
- Runtime: Docker
- Dockerfile: `Dockerfile`
- Docker context: `.`
- Health: `/health`

Both services use the same GitHub repository and the same MongoDB database. The main bot sends signed Premium-only URLs to the streaming service.

## Working product behavior kept intact
- Free and Premium user flows
- Mandatory Force Subscribe
- Request group: `https://t.me/moviesearchoffc`
- Cinematic search status message
- Smart/fuzzy search and result pagination
- 10-minute request/result cleanup
- Softurl verification for Free users
- Premium plans and screenshot approval/rejection workflow
- Premium membership/tag/status
- Direct Premium file delivery
- Signed Premium streaming links

## Performance improvements
The main bot keeps the previous UI/business logic and adds a bounded, indexed search layer:
- cached distinct-title candidates
- indexed `search_tokens`
- background token backfill
- short-lived search result/count caching
- bounded concurrent MongoDB search work
- database indexes for title/quality/language/season ordering

## Streaming architecture
- Premium authorization is checked against MongoDB on every signed request.
- Telegram media is streamed through HTTP Range requests rather than fully copied to the server for normal playback/download.
- Browser-compatible MP4/H264-style media uses direct Range playback.
- MKV/HEVC/x265-style media can use a compatibility HLS pipeline that feeds Telegram media progressively into FFmpeg instead of waiting for a full source download first.
- Compatibility transcoding is limited to one concurrent job by default on the free Render plan.
- Player includes Download, VLC, MX Player, share/copy actions and a clean Cinema HUB OG presentation.

## Free-plan reality
Large HEVC/MKV compatibility conversion is CPU-intensive. The architecture avoids unnecessary full-file buffering, but free CPU can still make a heavy transcode slow. This is a hardware limitation, not a hidden promise of real-time 1080p transcoding on free compute.

## Critical environment relationship
The main bot must use:
- `STREAM_BASE_URL=<the streaming Render service URL>`
- `STREAM_SIGNING_SECRET=<exactly the same value configured on the streaming service>`
- `STREAM_TOKEN_TTL_SECONDS=1800`

The streaming service must use the same `MONGO_URI`, `DB_NAME`, Telegram API credentials, database-channel ID, and signing secret that correspond to the main bot deployment.

See `FINAL_DEPLOYMENT.md` for the exact deployment sequence and environment-variable checklist.
