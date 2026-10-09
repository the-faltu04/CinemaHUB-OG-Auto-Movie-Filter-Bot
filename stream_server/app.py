import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from bson import ObjectId
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pymongo import AsyncMongoClient
from telethon import TelegramClient
from telethon.sessions import StringSession

APP = FastAPI(title="Cinema HUB OG Stream Server", version="6.0")
log = logging.getLogger("cinema_hub_stream")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("DATABASE_URI") or ""
DB_NAME = os.getenv("DB_NAME") or os.getenv("DATABASE_NAME") or "autofilter"
API_ID_RAW = os.getenv("API_ID", "0") or "0"
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING_RAW = os.getenv("STREAM_SESSION_STRING") or os.getenv("SESSION_STRING") or ""
STREAM_SECRET = os.getenv("STREAM_SIGNING_SECRET", "")
DATABASE_CHANNEL = os.getenv("STREAM_DATABASE_CHANNEL_ID") or os.getenv("DATABASE_CHANNEL_ID") or os.getenv("BIN_CHANNEL") or ""
PORT = int(os.getenv("PORT", "10000") or "10000")

CACHE_ROOT = Path(os.getenv("STREAM_CACHE_DIR", "/tmp/cinema-hub-stream-cache"))
ASSET_ROOT = Path(__file__).resolve().parent / "assets"
LOCAL_LOGO = ASSET_ROOT / "cinema_hub_og_logo.jpg"
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
STREAM_POWERED_BY = os.getenv("STREAM_POWERED_BY", "Cinema HUB OG") or "Cinema HUB OG"
STREAM_SERVICE_BY = os.getenv("STREAM_SERVICE_BY", "The Visionary Team") or "The Visionary Team"
STREAM_LOGO_URL = os.getenv("STREAM_LOGO_URL", "") or ""
CACHE_MAX_AGE_SECONDS = int(os.getenv("STREAM_CACHE_MAX_AGE_SECONDS", "21600") or "21600")
TRANSCODE_CONCURRENCY = max(1, min(int(os.getenv("STREAM_TRANSCODE_CONCURRENCY", "1") or "1"), 2))
MEDIA_CONCURRENCY = max(2, min(int(os.getenv("STREAM_MEDIA_CONCURRENCY", "6") or "6"), 12))
# Telegram/MTProto file downloads are most efficient around 512 KiB; Telethon
# documents a 512 KiB maximum request size. cite:turn874435search1
STREAM_CHUNK_SIZE = 512 * 1024
HLS_TIME = max(4, min(int(os.getenv("HLS_SEGMENT_SECONDS", "6") or "6"), 10))

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    API_ID = 0

CACHE_ROOT.mkdir(parents=True, exist_ok=True)
ASSET_ROOT.mkdir(parents=True, exist_ok=True)
mongo = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=10000) if MONGO_URI else None
db = mongo[DB_NAME] if mongo else None

client = None
client_lock = asyncio.Lock()
client_error = None
cleanup_task = None
cache_locks: dict[str, asyncio.Lock] = {}
hls_jobs: dict[str, asyncio.Task] = {}
hls_processes: dict[str, asyncio.subprocess.Process] = {}
hls_status: dict[str, dict] = {}
hls_semaphore = asyncio.Semaphore(TRANSCODE_CONCURRENCY)
media_semaphore = asyncio.Semaphore(MEDIA_CONCURRENCY)
message_cache: dict[str, tuple[float, object]] = {}


def valid_session_string(value: str) -> str:
    value = (value or "").strip().replace(" ", "")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    if not value or not value.startswith("1"):
        return ""
    try:
        payload = value[1:] + "=" * (-len(value[1:]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        if len(decoded) not in {263, 275}:
            return ""
        return value
    except Exception:
        return ""


SESSION_STRING = valid_session_string(SESSION_STRING_RAW)
if SESSION_STRING_RAW and not SESSION_STRING:
    client_error = "STREAM_SESSION_STRING is malformed"
    log.error(client_error)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_movie_lock(key: str) -> asyncio.Lock:
    lock = cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        cache_locks[key] = lock
    return lock


def decode_token(token: str) -> dict:
    if not STREAM_SECRET or "." not in token:
        raise HTTPException(status_code=403, detail="Invalid stream token")
    raw, sig = token.rsplit(".", 1)
    expected = hmac.new(STREAM_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=403, detail="Invalid stream token")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Invalid stream token") from exc
    try:
        expiry = int(payload.get("e", 0))
        user_id = int(payload.get("u"))
        movie_id = str(payload.get("m"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Invalid stream token") from exc
    if expiry <= int(now_utc().timestamp()):
        raise HTTPException(status_code=403, detail="Stream token expired")
    if len(movie_id) != 24:
        raise HTTPException(status_code=403, detail="Invalid movie token")
    return {"e": expiry, "u": user_id, "m": movie_id}


def premium_until_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except Exception:
            return None
    return None


async def ensure_telegram() -> TelegramClient:
    global client, client_error
    if client is not None and client.is_connected():
        return client
    if not SESSION_STRING or not API_ID or not API_HASH:
        raise HTTPException(status_code=503, detail="Streaming Telegram connection is not configured")
    async with client_lock:
        if client is not None and client.is_connected():
            return client
        try:
            candidate = TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH,
                receive_updates=False,
                entity_cache_limit=500,
            )
            await asyncio.wait_for(candidate.connect(), timeout=30)
            authorized = await asyncio.wait_for(candidate.is_user_authorized(), timeout=15)
            if not authorized:
                await candidate.disconnect()
                raise RuntimeError("STREAM_SESSION_STRING is not authorized")
            client = candidate
            client_error = None
            log.info("Telegram streaming client connected.")
            return client
        except Exception as exc:
            client = None
            client_error = str(exc)
            log.error("Telegram streaming connection failed: %s", exc)
            raise HTTPException(status_code=503, detail="Telegram streaming connection is temporarily unavailable") from exc


async def get_context(token: str):
    payload = decode_token(token)
    if db is None or mongo is None:
        raise HTTPException(status_code=503, detail="Streaming database is not configured")
    try:
        user = await db.users.find_one({"user_id": payload["u"]}, {"premium_until": 1})
    except Exception as exc:
        log.warning("Premium lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="Streaming database is temporarily unavailable") from exc
    until = premium_until_datetime((user or {}).get("premium_until"))
    if until is None or until <= now_utc():
        raise HTTPException(status_code=403, detail="Premium access required")
    try:
        movie = await db.movies.find_one({"_id": ObjectId(payload["m"])})
    except Exception as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    if not movie:
        raise HTTPException(status_code=404, detail="File not found")
    return payload, movie


def filename_for(movie: dict) -> str:
    value = str(movie.get("filename") or movie.get("title") or "cinema-hub-media").strip()
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", value) or "cinema-hub-media"


def file_size_for(movie: dict) -> int:
    for key in ("file_size", "size"):
        try:
            value = int(movie.get(key) or 0)
            if value > 0:
                return value
        except Exception:
            pass
    return 0


def mime_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
    }.get(ext, "application/octet-stream")


def likely_needs_compatibility(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in (".mkv", ".avi", ".wmv", ".flv", "hevc", "h265", "x265"))


def likely_fast_remux(name: str) -> bool:
    low = name.lower()
    if any(marker in low for marker in ("hevc", "h265", "x265")):
        return False
    return any(marker in low for marker in ("x264", "h264", "avc", ".mkv", ".mov", ".avi"))


def cache_key_for(movie: dict) -> str:
    return hashlib.sha256(
        str(movie.get("_id") or movie.get("message_id") or movie.get("filename") or "movie").encode()
    ).hexdigest()


async def get_message(movie: dict):
    tg = await ensure_telegram()
    chat_id = movie.get("chat_id") or DATABASE_CHANNEL
    message_id = movie.get("message_id")
    if not chat_id or not message_id:
        raise HTTPException(status_code=404, detail="Telegram source is unavailable")
    cache_key = f"{chat_id}:{message_id}"
    cached = message_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 300:
        msg = cached[1]
        if getattr(msg, "media", None):
            return msg
    try:
        msg = await asyncio.wait_for(
            tg.get_messages(
                int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                ids=int(message_id),
            ),
            timeout=30,
        )
    except Exception as exc:
        log.warning("Telegram media lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="Telegram media is temporarily unavailable") from exc
    if not msg or not msg.media:
        raise HTTPException(status_code=404, detail="Telegram media is unavailable")
    message_cache[cache_key] = (time.monotonic(), msg)
    return msg


def actual_media_size(msg, fallback: int) -> int:
    try:
        size = int(getattr(getattr(msg, "file", None), "size", 0) or 0)
        if size > 0:
            return size
    except Exception:
        pass
    try:
        size = int(getattr(getattr(msg.media, "document", None), "size", 0) or 0)
        if size > 0:
            return size
    except Exception:
        pass
    return max(0, int(fallback or 0))


def parse_range(header: str | None, size: int):
    if size <= 0:
        raise HTTPException(status_code=416, detail="Range Not Satisfiable")
    if not header:
        return 0, size - 1, False
    if not header.startswith("bytes="):
        raise HTTPException(status_code=416, detail="Invalid Range", headers={"Content-Range": f"bytes */{size}"})
    value = header[6:].split(",", 1)[0].strip()
    if "-" not in value:
        raise HTTPException(status_code=416, detail="Invalid Range", headers={"Content-Range": f"bytes */{size}"})
    start_s, end_s = value.split("-", 1)
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
            if start < 0 or start >= size:
                raise ValueError
            end = min(end, size - 1)
        if end < start:
            raise ValueError
        return start, end, True
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid Range", headers={"Content-Range": f"bytes */{size}"}) from exc


async def iter_range(msg, start: int, length: int):
    tg = await ensure_telegram()
    remaining = int(length)
    position = int(start)
    retries = 0
    if remaining <= 0:
        return
    async with media_semaphore:
        while remaining > 0:
            stream = tg.iter_download(
                msg.media,
                offset=position,
                request_size=STREAM_CHUNK_SIZE,
                chunk_size=STREAM_CHUNK_SIZE,
            )
            completed = False
            try:
                async for data in stream:
                    if not data:
                        continue
                    out = bytes(data)
                    if len(out) > remaining:
                        out = out[:remaining]
                    if out:
                        position += len(out)
                        remaining -= len(out)
                        retries = 0
                        yield out
                    if remaining <= 0:
                        completed = True
                        break
                if remaining <= 0:
                    completed = True
            except Exception as exc:
                retries += 1
                log.warning("Telegram range stream interrupted at %s (%s): %s", position, retries, exc)
                if retries > 3:
                    raise
                await asyncio.sleep(min(2 * retries, 5))
            finally:
                close = getattr(stream, "close", None)
                if close:
                    try:
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass
            if completed:
                return


async def media_response(token: str, range_header: str | None, attachment: bool = False):
    _, movie = await get_context(token)
    msg = await get_message(movie)
    name = filename_for(movie)
    size = actual_media_size(msg, file_size_for(movie))
    start, end, partial = parse_range(range_header, size)
    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": mime_for(name),
        "Cache-Control": "private, no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
        "X-Accel-Buffering": "no",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    disposition = "attachment" if attachment else "inline"
    safe_name = name.replace('"', "'")
    headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    return StreamingResponse(
        iter_range(msg, start, length),
        status_code=206 if partial else 200,
        headers=headers,
    )


async def _ffmpeg_error_reader(process):
    if process.stderr is None:
        return ""
    data = await process.stderr.read()
    return data.decode("utf-8", "ignore")[-3000:]


async def ffmpeg_compat_stream(msg, *, remux: bool):
    """Progressively turn MKV/other containers into browser-playable fragmented MP4.

    Remux mode copies the video stream when the filename strongly suggests H.264/AVC.
    Transcode mode converts the video to H.264 with an intentionally fast preset for
    the zero-cost Render plan. Audio is normalized to AAC in both paths.
    """
    if remux:
        video_args = ["-c:v", "copy"]
        mode = "copy-remux"
    else:
        video_args = [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-crf", "29",
            "-pix_fmt", "yuv420p",
            "-threads", "1",
        ]
        mode = "h264-transcode"

    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-fflags", "+genpts",
        "-analyzeduration", "3M",
        "-probesize", "3M",
        "-i", "pipe:0",
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        *video_args,
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def feed():
        tg = await ensure_telegram()
        stream = tg.iter_download(msg.media, request_size=STREAM_CHUNK_SIZE, chunk_size=STREAM_CHUNK_SIZE)
        try:
            async for data in stream:
                if process.returncode is not None:
                    break
                if not data:
                    continue
                try:
                    process.stdin.write(bytes(data))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            close = getattr(stream, "close", None)
            if close:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except Exception:
                    pass

    feeder = asyncio.create_task(feed(), name=f"cinema-feed-{mode}")
    stderr_task = asyncio.create_task(_ffmpeg_error_reader(process), name=f"cinema-stderr-{mode}")
    try:
        while True:
            chunk = await process.stdout.read(256 * 1024)
            if not chunk:
                break
            yield chunk
        await process.wait()
        error_text = await stderr_task
        if process.returncode != 0:
            raise RuntimeError(error_text or "Browser compatibility conversion failed.")
    finally:
        if not feeder.done():
            feeder.cancel()
        try:
            await feeder
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass


async def start_hls_job(movie: dict):
    key = cache_key_for(movie)
    existing = hls_jobs.get(key)
    if existing and not existing.done():
        return
    hls_jobs[key] = asyncio.create_task(transcode_to_hls(movie), name=f"cinema-hls-{key[:8]}")


async def transcode_to_hls(movie: dict):
    key = cache_key_for(movie)
    root = CACHE_ROOT / key
    hls_dir = root / "hls"
    playlist = hls_dir / "index.m3u8"
    hls_dir.mkdir(parents=True, exist_ok=True)
    if playlist.exists() and any(hls_dir.glob("segment_*.ts")):
        hls_status[key] = {"state": "ready", "message": "Compatibility stream ready.", "updated_at": now_utc().isoformat()}
        return
    async with hls_semaphore:
        hls_status[key] = {"state": "preparing", "message": "Preparing browser-compatible stream…", "updated_at": now_utc().isoformat()}
        tg = await ensure_telegram()
        msg = await get_message(movie)
        playlist.unlink(missing_ok=True)
        for p in hls_dir.glob("segment_*.ts"):
            p.unlink(missing_ok=True)
        cmd = [
            FFMPEG_BIN,
            "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts",
            "-analyzeduration", "5M", "-probesize", "5M",
            "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "29",
            "-pix_fmt", "yuv420p", "-threads", "1",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-force_key_frames", f"expr:gte(t,n_forced*{HLS_TIME})",
            "-f", "hls", "-hls_time", str(HLS_TIME), "-hls_list_size", "0",
            "-hls_playlist_type", "event", "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_filename", str(hls_dir / "segment_%05d.ts"), str(playlist),
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        hls_processes[key] = process

        async def feed_hls():
            stream = tg.iter_download(msg.media, request_size=STREAM_CHUNK_SIZE, chunk_size=STREAM_CHUNK_SIZE)
            try:
                async for data in stream:
                    if process.returncode is not None:
                        break
                    if not data:
                        continue
                    process.stdin.write(bytes(data))
                    await process.stdin.drain()
            finally:
                close = getattr(stream, "close", None)
                if close:
                    try:
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass
                if process.stdin and not process.stdin.is_closing():
                    process.stdin.close()

        feeder = asyncio.create_task(feed_hls())
        stderr = await process.stderr.read()
        await process.wait()
        await feeder
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "ignore")[-2500:] or "FFmpeg compatibility conversion failed."
            hls_status[key] = {"state": "error", "message": detail, "updated_at": now_utc().isoformat()}
        elif playlist.exists() and any(hls_dir.glob("segment_*.ts")):
            hls_status[key] = {"state": "ready", "message": "Compatibility stream ready.", "updated_at": now_utc().isoformat()}
        else:
            hls_status[key] = {"state": "error", "message": "FFmpeg did not create a playable stream.", "updated_at": now_utc().isoformat()}
        hls_processes.pop(key, None)


async def cleanup_cache():
    while True:
        try:
            cutoff = time.time() - CACHE_MAX_AGE_SECONDS
            for child in CACHE_ROOT.iterdir():
                if not child.is_dir():
                    continue
                key = child.name
                if key in hls_processes:
                    continue
                try:
                    if child.stat().st_mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                        hls_status.pop(key, None)
                except OSError:
                    pass
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Cache cleanup failed")
            await asyncio.sleep(60)


async def ensure_brand_logo():
    if STREAM_LOGO_URL:
        return STREAM_LOGO_URL
    if LOCAL_LOGO.exists():
        return "/brand/logo"
    return ""


@APP.on_event("startup")
async def startup():
    global cleanup_task
    cleanup_task = asyncio.create_task(cleanup_cache(), name="stream-cache-cleanup")
    log.info("Cinema HUB OG stream server started on port %s", PORT)


@APP.on_event("shutdown")
async def shutdown():
    global client
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
    for proc in list(hls_processes.values()):
        try:
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
    for task in list(hls_jobs.values()):
        if not task.done():
            task.cancel()
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
    if mongo is not None:
        await mongo.close()


@APP.get("/")
async def root():
    return {"status": "ok", "service": "cinema-hub-og-stream", "mode": "render-free-range"}


@APP.get("/health")
async def health():
    database_ready = False
    if db and mongo:
        try:
            await mongo.admin.command("ping")
            database_ready = True
        except Exception:
            pass
    ffmpeg_ready = shutil.which(FFMPEG_BIN) is not None
    configured = bool(MONGO_URI and STREAM_SECRET and SESSION_STRING and API_ID and API_HASH and DATABASE_CHANNEL)
    # Telegram connection is intentionally lazy, so a healthy service doesn't need
    # to establish a Telethon session during health checks.
    status = "ok" if database_ready and ffmpeg_ready and configured else "degraded"
    return {
        "status": status,
        "database": database_ready,
        "telegram": bool(client and client.is_connected()),
        "telegram_authorized": bool(client and client.is_connected()),
        "ffmpeg": ffmpeg_ready,
        "configured": configured,
        "database_name": DB_NAME,
        "telegram_error": client_error or "",
    }


@APP.get("/readyz")
async def readyz():
    check = await health()
    if check["status"] != "ok":
        return JSONResponse(check, status_code=503)
    return JSONResponse(check, status_code=200)


@APP.get("/api/meta/{token}")
async def meta(token: str):
    _, movie = await get_context(token)
    name = filename_for(movie)
    return {
        "name": name,
        "size": file_size_for(movie),
        "mime": mime_for(name),
        "needs_compatibility": likely_needs_compatibility(name),
        "fast_remux": likely_fast_remux(name),
        "download": f"/download/{token}",
        "direct": f"/media/{token}",
        "compat": f"/compat/{token}",
        "hls": f"/hls/{token}/index.m3u8",
        "hls_status": hls_status.get(cache_key_for(movie), {"state": "idle", "message": "Not started."}),
    }


@APP.post("/api/hls/prepare/{token}")
async def hls_prepare(token: str):
    _, movie = await get_context(token)
    await start_hls_job(movie)
    key = cache_key_for(movie)
    return {"started": True, **hls_status.get(key, {"state": "queued", "message": "Compatibility job queued."})}


@APP.get("/api/hls/status/{token}")
async def hls_status_api(token: str):
    _, movie = await get_context(token)
    key = cache_key_for(movie)
    hls_dir = CACHE_ROOT / key / "hls"
    playlist = hls_dir / "index.m3u8"
    segments = len(list(hls_dir.glob("segment_*.ts"))) if hls_dir.exists() else 0
    state = hls_status.get(key, {"state": "idle", "message": "Not started."})
    ready = playlist.exists() and playlist.stat().st_size > 200 and segments > 0
    return {"ready": ready, "segments": segments, **state}


@APP.get("/brand/logo")
async def brand_logo():
    if STREAM_LOGO_URL:
        return Response(status_code=307, headers={"Location": STREAM_LOGO_URL})
    if LOCAL_LOGO.exists():
        return FileResponse(str(LOCAL_LOGO), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="Brand logo unavailable")


@APP.get("/media/{token}")
async def media(token: str, range_header: str | None = Header(default=None, alias="Range")):
    return await media_response(token, range_header, attachment=False)


@APP.head("/media/{token}")
async def media_head(token: str, range_header: str | None = Header(default=None, alias="Range")):
    _, movie = await get_context(token)
    msg = await get_message(movie)
    name = filename_for(movie)
    size = actual_media_size(msg, file_size_for(movie))
    start, end, partial = parse_range(range_header, size)
    length = end - start + 1
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Content-Type": mime_for(name)}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(status_code=206 if partial else 200, headers=headers)


@APP.get("/download/{token}")
async def download(token: str, range_header: str | None = Header(default=None, alias="Range")):
    return await media_response(token, range_header, attachment=True)


@APP.get("/compat/{token}")
async def compatibility_stream(token: str):
    _, movie = await get_context(token)
    msg = await get_message(movie)
    name = filename_for(movie)
    mode = "copy-remux" if likely_fast_remux(name) else "h264-transcode"
    output_name = Path(name).stem + ".mp4"
    headers = {
        "Cache-Control": "private, no-store, max-age=0",
        "Content-Disposition": f'inline; filename="{output_name.replace(chr(34), "'")}"',
        "X-Cinema-HUB-Compatibility": mode,
        "X-Content-Type-Options": "nosniff",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        ffmpeg_compat_stream(msg, remux=(mode == "copy-remux")),
        media_type="video/mp4",
        headers=headers,
    )


@APP.get("/hls/{token}/index.m3u8")
async def hls_playlist(token: str):
    _, movie = await get_context(token)
    key = cache_key_for(movie)
    playlist = CACHE_ROOT / key / "hls" / "index.m3u8"
    if not playlist.exists() or playlist.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="Compatibility stream is still preparing")
    return FileResponse(str(playlist), media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@APP.get("/hls/{token}/{asset:path}")
async def hls_asset(token: str, asset: str):
    _, movie = await get_context(token)
    if "/" in asset or ".." in asset:
        raise HTTPException(status_code=400, detail="Invalid HLS asset")
    path = CACHE_ROOT / cache_key_for(movie) / "hls" / asset
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="HLS segment unavailable")
    media_type = "video/mp2t" if path.suffix.lower() == ".ts" else "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, headers={"Cache-Control": "public, max-age=30"})


@APP.get("/watch/{token}", response_class=HTMLResponse)
async def watch(token: str):
    _, movie = await get_context(token)
    name = filename_for(movie)
    size = file_size_for(movie)
    metadata_bits = [mime_for(name), format_size(size)]
    if movie.get("quality"):
        metadata_bits.insert(0, str(movie.get("quality")))
    if movie.get("language"):
        metadata_bits.append(str(movie.get("language")))
    metadata = " • ".join([x for x in metadata_bits if x and x != "application/octet-stream"])
    direct = f"/media/{quote(token, safe='')}"
    download_url = f"/download/{quote(token, safe='')}"
    compat_url = f"/compat/{quote(token, safe='')}"
    hls = f"/hls/{quote(token, safe='')}/index.m3u8"
    hls_prepare = f"/api/hls/prepare/{quote(token, safe='')}"
    hls_status_url = f"/api/hls/status/{quote(token, safe='')}"
    logo = "/brand/logo"
    needs = likely_needs_compatibility(name)
    fast_remux = likely_fast_remux(name)
    title_json = json.dumps(name)
    template = """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#05090d\"><title>Cinema HUB OG — __TITLE__</title><link rel=\"icon\" href=\"__LOGO__\"><style>
:root{--bg:#05090d;--panel:#0a141c;--panel2:#0e1d27;--line:rgba(255,255,255,.09);--text:#f3f7fa;--muted:#91a2ae;--accent:#5fe3d4;--accent2:#21b7ad}
*{box-sizing:border-box}html{color-scheme:dark}body{margin:0;min-height:100vh;background:radial-gradient(circle at 50% -5%,rgba(95,227,212,.13),transparent 34%),linear-gradient(180deg,#071117 0%,#05090d 58%,#04070a 100%);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}.wrap{width:min(900px,100%);margin:auto;padding:18px 14px 44px}.brand{display:flex;align-items:center;gap:13px;padding:4px 2px 17px}.brand img{width:56px;height:56px;border-radius:16px;object-fit:contain;background:#000;border:1px solid rgba(255,255,255,.1);box-shadow:0 12px 32px rgba(0,0,0,.35)}.brand h1{margin:0;font-size:18px;letter-spacing:.16em;font-weight:950}.brand p{margin:5px 0 0;color:#9eb0bb;font-size:12px}.card{border:1px solid var(--line);border-radius:25px;overflow:hidden;background:linear-gradient(180deg,rgba(12,25,34,.97),rgba(7,16,22,.98));box-shadow:0 28px 90px rgba(0,0,0,.38)}.video-shell{position:relative;background:#000;aspect-ratio:16/9}.video-shell video{width:100%;height:100%;display:block;background:#000;object-fit:contain}.overlay{position:absolute;inset:0;display:grid;place-items:center;background:linear-gradient(180deg,rgba(0,0,0,.10),rgba(0,0,0,.46));pointer-events:none}.loader{width:min(350px,90%);padding:18px 17px;text-align:center;border-radius:17px;border:1px solid rgba(255,255,255,.09);background:rgba(4,9,13,.78);backdrop-filter:blur(16px)}.spinner{width:30px;height:30px;margin:0 auto 10px;border:3px solid rgba(255,255,255,.15);border-top-color:var(--accent);border-radius:50%;animation:spin .9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.body{padding:18px}.title{font-size:clamp(17px,3vw,25px);font-weight:900;line-height:1.28;word-break:break-word}.meta{margin-top:7px;color:var(--muted);font-size:12px;line-height:1.5}.status{margin-top:13px;padding:9px 11px;text-align:center;border-radius:11px;border:1px solid var(--line);background:rgba(255,255,255,.025);color:#b5c3cc;font-size:10px;font-weight:850;letter-spacing:.08em;text-transform:uppercase}.status.good{color:#baf8f0;border-color:rgba(95,227,212,.25);background:rgba(95,227,212,.07)}.status.bad{color:#ffd0d0;border-color:rgba(255,100,100,.2);background:rgba(255,100,100,.06)}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:9px;margin-top:12px}.btn{min-height:46px;border-radius:13px;padding:11px 12px;border:1px solid var(--line);background:#13232d;color:var(--text);text-decoration:none;display:flex;align-items:center;justify-content:center;gap:8px;font-weight:850;cursor:pointer;transition:transform .18s ease,background .18s ease,border-color .18s ease}.btn:active{transform:scale(.985)}.btn.primary{background:linear-gradient(135deg,#d9fffa,#39c9bb);color:#061315;border:none}.btn.alt{background:#10202a}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:9px}.mini .btn{min-height:43px;font-size:12px}.tiny{display:none;color:#899aa4;font-size:11px;text-align:center;margin-top:10px}.tiny.show{display:block}.hint{color:#788a95;font-size:11px;line-height:1.6;text-align:center;margin-top:12px}.footer{text-align:center;color:#63747d;font-size:11px;margin-top:16px;letter-spacing:.03em}@media(max-width:540px){.grid{grid-template-columns:1fr}.mini{grid-template-columns:repeat(3,1fr)}}
</style></head><body><main class=\"wrap\"><header class=\"brand\"><img src=\"__LOGO__\" alt=\"Cinema HUB OG\"><div><h1>CINEMA HUB OG</h1><p>Your cinema. Instantly on screen.</p></div></header><section class=\"card\"><div class=\"video-shell\"><video id=\"player\" controls playsinline preload=\"metadata\" poster=\"__LOGO__\"></video><div class=\"overlay\" id=\"overlay\"><div class=\"loader\"><div class=\"spinner\"></div><b id=\"overlayTitle\">Secure playback</b><div id=\"overlayText\" style=\"margin-top:6px;color:#9babb6;font-size:12px\">Checking the media format…</div></div></div></div><div class=\"body\"><div class=\"title\">__TITLE__</div><div class=\"meta\">__META__</div><div class=\"status\" id=\"status\">SECURE PREMIUM SESSION</div><div class=\"grid\"><button class=\"btn primary\" id=\"play\">▶ PLAY IN BROWSER</button><a class=\"btn\" href=\"__DOWNLOAD__\">⬇ DOWNLOAD</a></div><div class=\"mini\"><button class=\"btn alt\" id=\"vlc\">VLC</button><button class=\"btn alt\" id=\"mx\">MX PLAYER</button><button class=\"btn alt\" id=\"share\">••• MORE</button></div><div class=\"grid\"><button class=\"btn alt\" id=\"copy\">📋 COPY STREAM URL</button><button class=\"btn alt\" id=\"compat\" style=\"__COMPAT_DISPLAY__\">▶ COMPATIBILITY</button></div><div class=\"tiny\" id=\"tip\"></div><div class=\"hint\">Premium members only • Authenticated access • Original Telegram media is never exposed directly.</div></div></section><div class=\"footer\">Powered By <b>__POWERED__</b> · Service By <b>__SERVICE__</b></div></main><script src=\"https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js\"></script><script>
const direct=__DIRECT_JSON__,downloadUrl=__DOWNLOAD_JSON__,compatUrl=__COMPAT_JSON__,hls=__HLS_JSON__,prepareUrl=__PREPARE_JSON__,statusUrl=__STATUS_JSON__,needs=__NEEDS_JSON__,fastRemux=__REMUX_JSON__;const player=document.getElementById('player'),overlay=document.getElementById('overlay'),ot=document.getElementById('overlayTitle'),ox=document.getElementById('overlayText'),status=document.getElementById('status'),play=document.getElementById('play'),compat=document.getElementById('compat'),tip=document.getElementById('tip');let hlsInstance=null,pendingPlay=false,statusTimer=null;
function setStatus(text,kind=''){status.textContent=text;status.className='status '+kind}function showOverlay(show,title='Secure playback',text='Checking the media format…'){overlay.style.display=show?'grid':'none';ot.textContent=title;ox.textContent=text}function tipText(t){tip.textContent=t;tip.classList.add('show')}function stopHls(){if(hlsInstance){try{hlsInstance.destroy()}catch(e){}hlsInstance=null}if(statusTimer)clearTimeout(statusTimer)}function playVideo(){player.play().catch(()=>{})}
function directPlay(){stopHls();pendingPlay=true;showOverlay(true,'Starting stream','Connecting directly to Telegram media…');player.src=direct;player.load();setStatus('DIRECT STREAM • STARTING');player.onloadedmetadata=()=>{showOverlay(false);setStatus('DIRECT STREAM • READY','good');playVideo()};}
function compatPlay(){stopHls();pendingPlay=true;showOverlay(true,'Preparing browser playback',fastRemux?'Fast container remux • no video re-encode':'Converting to browser-compatible H.264…');setStatus('COMPATIBILITY • STARTING');player.src=compatUrl;player.load();player.onloadedmetadata=()=>{showOverlay(false);setStatus(fastRemux?'COMPATIBILITY • READY':'H.264 COMPATIBILITY • READY','good');playVideo()};}
function hlsPlay(){stopHls();pendingPlay=true;showOverlay(true,'Preparing compatibility stream','Creating the first playable HLS segments…');setStatus('HLS • PREPARING…');fetch(prepareUrl,{method:'POST',cache:'no-store'}).catch(()=>{});pollHls();}
async function pollHls(){try{const r=await fetch(statusUrl,{cache:'no-store'}),d=await r.json();if(d.ready){if(window.Hls&&Hls.isSupported()){hlsInstance=new Hls({enableWorker:true,backBufferLength:30,maxBufferLength:30,manifestLoadingRetryDelay:1200,levelLoadingRetryDelay:1200,fragLoadingRetryDelay:1200});hlsInstance.loadSource(hls);hlsInstance.attachMedia(player);hlsInstance.on(Hls.Events.MANIFEST_PARSED,()=>{showOverlay(false);setStatus('HLS COMPATIBILITY • READY','good');playVideo()});hlsInstance.on(Hls.Events.ERROR,(e,x)=>{if(x&&x.fatal)setStatus('HLS • RETRYING…')});return}if(player.canPlayType('application/vnd.apple.mpegurl')){player.src=hls;player.load();showOverlay(false);setStatus('HLS COMPATIBILITY • READY','good');playVideo();return}setStatus('HLS NOT SUPPORTED','bad');showOverlay(false);return}setStatus('HLS • '+String(d.state||'QUEUED').replaceAll('_',' '));showOverlay(true,'Preparing compatibility stream',d.message||'Waiting for playable segments…')}catch(e){setStatus('STREAM SERVER • RETRYING…')}statusTimer=setTimeout(pollHls,1600)}
play.onclick=()=>{if(needs){compatPlay()}else{directPlay()}};compat.onclick=()=>{if(needs)hlsPlay()};player.addEventListener('error',()=>{if(needs){setStatus('BROWSER PLAYBACK UNSUPPORTED','bad');showOverlay(false);tipText('Try VLC / MX Player for original media, or use Compatibility for browser playback.');compat.style.display='flex'}else{setStatus('DIRECT STREAM FAILED','bad');showOverlay(false)}});
document.getElementById('copy').onclick=async()=>{try{await navigator.clipboard.writeText(new URL(direct,location.href).href);setStatus('STREAM URL COPIED','good');tipText('Paste this URL into VLC, MX Player or another media player.')}catch(e){setStatus('COPY NOT AVAILABLE','bad')}};
document.getElementById('share').onclick=async()=>{const u=new URL(direct,location.href).href;try{if(navigator.share)await navigator.share({title:__TITLE_JSON__,url:u});else{await navigator.clipboard.writeText(u);tipText('Stream URL copied.')}}catch(e){}};
function launchExternal(pkg,scheme,label){const absolute=new URL(direct,location.href).href;const u=new URL(absolute);const android=/Android/i.test(navigator.userAgent);if(android){const intent='intent://'+u.host+u.pathname+u.search+'#Intent;scheme=https;action=android.intent.action.VIEW;category=android.intent.category.BROWSABLE;package='+pkg+';type=video/*;S.browser_fallback_url='+encodeURIComponent(absolute)+';end';let wentAway=false;const onVis=()=>{if(document.hidden)wentAway=true};document.addEventListener('visibilitychange',onVis);window.location.href=intent;setTimeout(()=>{document.removeEventListener('visibilitychange',onVis);if(!wentAway)tipText(label+' app was not opened. The authenticated web stream URL is ready below.')},1500);return}if(scheme){window.location.href=scheme+absolute}else{navigator.clipboard?.writeText(absolute);tipText('Stream URL copied for '+label+'.')}}
document.getElementById('vlc').onclick=()=>launchExternal('org.videolan.vlc','vlc://','VLC');document.getElementById('mx').onclick=()=>launchExternal('com.mxtech.videoplayer.ad','mxplayer://','MX Player');
if(needs){showOverlay(false);setStatus(fastRemux?'BROWSER COMPATIBILITY • READY':'H.264 COMPATIBILITY • READY');tipText('Original media is ready instantly in VLC/MX Player. Browser playback uses a separate compatibility path.')}else{directPlay()}
</script></body></html>"""
    html = template.replace("__TITLE__", escape(name)).replace("__LOGO__", logo).replace("__META__", escape(metadata)).replace("__DOWNLOAD__", download_url).replace("__POWERED__", escape(STREAM_POWERED_BY)).replace("__SERVICE__", escape(STREAM_SERVICE_BY)).replace("__DIRECT_JSON__", json.dumps(direct)).replace("__DOWNLOAD_JSON__", json.dumps(download_url)).replace("__COMPAT_JSON__", json.dumps(compat_url)).replace("__HLS_JSON__", json.dumps(hls)).replace("__PREPARE_JSON__", json.dumps(hls_prepare)).replace("__STATUS_JSON__", json.dumps(hls_status_url)).replace("__NEEDS_JSON__", json.dumps(needs)).replace("__REMUX_JSON__", json.dumps(fast_remux)).replace("__COMPAT_DISPLAY__", "flex" if needs else "none")
    return HTMLResponse(html)


def format_size(value):
    try:
        value = float(value)
    except Exception:
        return ""
    if value >= 1024**3:
        return f"{value/(1024**3):.2f} GB"
    if value >= 1024**2:
        return f"{value/(1024**2):.2f} MB"
    if value >= 1024:
        return f"{value/1024:.2f} KB"
    return f"{int(value)} B"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(APP, host="0.0.0.0", port=PORT)
