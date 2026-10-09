import asyncio
import base64
import binascii
import logging
import os
import re
import struct
import secrets
import random
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from html import escape
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from fastapi import FastAPI
from pymongo import AsyncMongoClient, ASCENDING, ReturnDocument, UpdateOne
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.error import BadRequest, Forbidden, TelegramError, RetryAfter, RetryAfter
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    TypeHandler,
    filters,
)
from telegram.helpers import create_deep_linked_url
from ingest import InternetArchiveIngestor
from premium_system import PremiumManager, PREMIUM_PLANS, premium_plans_text, premium_plans_keyboard, payment_text, payment_keyboard, PREMIUM_ONLY_ALERT, NOT_ADDED_MESSAGE
from title_lookup import internet_title_exists

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:
    TelegramClient = None
    StringSession = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("autofilter")


# ---------------------------
# Configuration
# ---------------------------

def env_first(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default


def as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def csv_list(value):
    if not value:
        return []
    return [x.strip() for x in str(value).split(",") if x.strip()]


def csv_ints(value):
    out = set()
    for x in csv_list(value):
        try:
            out.add(int(x))
        except ValueError:
            log.warning("Ignoring invalid integer in list: %s", x)
    return out


PLACEHOLDER_VALUES = {"blank", "none", "null", "n/a", "na", "-"}

def is_placeholder(value):
    return str(value).strip().lower() in PLACEHOLDER_VALUES

def require_env(*names):
    value = env_first(*names)
    if value is None or is_placeholder(value):
        raise RuntimeError(f"Missing/invalid required environment variable: {names[0]}")
    return value

def optional_int(value, default=None, name="value"):
    if value is None or str(value).strip() == "" or is_placeholder(value):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number or left blank; got: {value!r}") from exc


def int_env(name, default, minimum=None, maximum=None):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "" or is_placeholder(raw):
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer or left blank; got: {raw!r}") from exc
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


class Config:
    def __init__(self):
        self.bot_token = require_env("BOT_TOKEN")
        self.api_id = optional_int(require_env("API_ID"), name="API_ID")
        self.api_hash = require_env("API_HASH")
        self.session_string = env_first("SESSION_STRING", default="")

        self.bot_username = (env_first("BOT_USERNAME", default="") or "").lstrip("@")
        self.admin_ids = csv_ints(env_first("ADMIN_IDS", "ADMINS", default=""))

        self.mongo_uri = require_env("MONGO_URI", "DATABASE_URI")
        self.db_name = env_first("DB_NAME", "DATABASE_NAME", default="autofilter")

        self.database_channel = require_env("DATABASE_CHANNEL_ID", "BIN_CHANNEL")
        # V4 locks the public request destination. Legacy REQUEST_GROUP_ID /
        # REQUEST_GROUP_USERNAME values are intentionally ignored so an old Render
        # value cannot redirect the user to a different or broken group.
        self.request_group_username = "moviesearchoffc"
        self.request_group = "@moviesearchoffc"


        self.fsub_channels = csv_list(env_first("FSUB_CHANNELS", default=""))
        self.fsub_links = csv_list(env_first("FSUB_INVITE_LINKS", default=""))

        # Softurl is the only shortener used for free-user verification.
        self.softurl_api = env_first("SOFTURL_API", default="")
        self.softurl_base_url = env_first(
            "SOFTURL_BASE_URL",
            default="https://softurl.in/api",
        )
        # Verification sessions are short-lived; completed verification grants 6 hours.
        self.verification_session_ttl = int_env("VERIFICATION_SESSION_TTL_SECONDS", 1800, 300, 86400)
        self.verification_access_ttl = int_env("VERIFICATION_ACCESS_TTL_SECONDS", 21600, 3600, 604800)
        try:
            self.start_delay_seconds = float(os.getenv("START_DELAY_SECONDS", "2.5") or "2.5")
        except ValueError:
            self.start_delay_seconds = 2.5
        # Keep the welcome-to-request transition short and consistent.
        self.start_delay_seconds = max(2.0, min(3.0, self.start_delay_seconds))

        # Premium/payment + streaming configuration.
        self.payment_bot_token = env_first("PAYMENT_BOT_TOKEN", default="")
        if self.payment_bot_token and self.payment_bot_token == self.bot_token:
            raise RuntimeError("PAYMENT_BOT_TOKEN must belong to a separate payment bot, not BOT_TOKEN.")
        self.payment_bot_force_polling = as_bool(env_first("PAYMENT_BOT_FORCE_POLLING", default="true"), True)
        self.payment_bot_username = (
            env_first("PAYMENT_BOT_USERNAME", default="visionaryowner_bot") or ""
        ).lstrip("@")
        self.premium_qr_url = env_first(
            "PREMIUM_QR_URL", default="https://t.me/+7_i3pMzJBTFlZTQ1"
        )
        self.stream_base_url = (env_first("STREAM_BASE_URL", default="") or "").rstrip("/")
        self.stream_signing_secret = env_first("STREAM_SIGNING_SECRET", default="")
        self.stream_token_ttl = int_env("STREAM_TOKEN_TTL_SECONDS", 1800, 300, 86400)
        self.premium_member_tag = env_first("PREMIUM_MEMBER_TAG", default="PREMIUM") or "PREMIUM"
        self.stream_powered_by = env_first("STREAM_POWERED_BY", default="Cinema HUB OG") or "Cinema HUB OG"
        self.stream_service_by = env_first("STREAM_SERVICE_BY", default="The Visionary Team") or "The Visionary Team"

        # Both new and legacy switches are understood.
        legacy_verify = env_first("IS_VERIFY", default=None)
        self.require_fsub = as_bool(
            env_first("REQUIRE_FSUB", default="true"), True
        )
        self.require_shortlink = as_bool(
            env_first(
                "REQUIRE_SHORTLINK",
                default=legacy_verify if legacy_verify is not None else "true",
            ),
            True,
        )

        self.shortlink_ttl = int_env("SHORTLINK_TTL_SECONDS", 1800, 60, 604800)
        self.delete_after = int_env("DELETE_AFTER_SECONDS", 300, 30, 86400)
        # Search result messages are ephemeral by design. This is intentionally
        # separate from DELETE_AFTER_SECONDS, which controls delivered movie files.
        # Default: exactly 10 minutes from the first appearance of the result message.
        self.search_result_delete_after = int_env(
            "SEARCH_RESULT_DELETE_AFTER_SECONDS", 600, 60, 86400
        )
        self.index_on_start = as_bool(
            env_first("INDEX_ON_START", default="false"), False
        )
        self.auto_index = as_bool(
            env_first("AUTO_INDEX_NEW_POSTS", default="true"), True
        )
        self.page_size = int_env("SEARCH_PAGE_SIZE", 8, 1, 10)
        # Kept for backwards-compatible Render environments. Search no longer
        # uses this value as a hard result ceiling; pagination is database-backed.
        self.max_results = optional_int(
            env_first("MAX_SEARCH_RESULTS"), default=None, name="MAX_SEARCH_RESULTS"
        )
        # SEND_ALL_LIMIT is a safety valve for bulk deliveries; search counts
        # are never capped by this value.
        self.send_all_limit = int_env("SEND_ALL_LIMIT", 1000, 1, 5000)
        self.index_batch_size = int_env("INDEX_BATCH_SIZE", 250, 50, 1000)
        self.caption_worker_interval = int_env("CAPTION_WORKER_INTERVAL_SECONDS", 2, 1, 60)
        self.caption_edit_min_interval = float(os.getenv("CAPTION_EDIT_MIN_INTERVAL_SECONDS", "1.1") or "1.1")

        # Authorized/public-domain automatic ingestion. Disabled by default
        # until the operator explicitly enables it in Render Environment.
        self.ia_ingest_enabled = as_bool(env_first("IA_INGEST_ENABLED", default="false"), False)
        self.ia_initial_backfill = as_bool(env_first("IA_INITIAL_BACKFILL", default="false"), False)
        self.ia_retry_skipped = as_bool(env_first("IA_RETRY_SKIPPED", default="false"), False)
        self.ia_scan_interval = int_env("IA_SCAN_INTERVAL_SECONDS", 1800, 300, 86400)
        self.ia_batch_size = int_env("IA_BATCH_SIZE", 5, 1, 100)
        self.ia_initial_pages = int_env("IA_INITIAL_PAGES", 5, 1, 1000)
        self.ia_scan_pages = int_env("IA_SCAN_PAGES", 10, 1, 1000)
        self.ia_initial_limit = int_env("IA_INITIAL_LIMIT", 100, 1, 5000)
        self.ia_page_size = int_env("IA_PAGE_SIZE", 50, 1, 100)
        self.ia_max_file_mb = int_env("IA_MAX_FILE_MB", 1800, 20, 1900)
        self.ia_http_timeout = int_env("IA_HTTP_TIMEOUT", 120, 30, 600)
        self.ia_query = env_first(
            "IA_QUERY",
            default="mediatype:movies",
        )
        self.developer_username = (env_first("DEVELOPER_USERNAME", default="thevisionaryoffc") or "").lstrip("@")
        self.developer_name = env_first("DEVELOPER_NAME", default="Cinema HUB OG Developer") or "Cinema HUB OG Developer"
        self.greeting_timezone = env_first("GREETING_TIMEZONE", default="Asia/Kolkata") or "Asia/Kolkata"
        # The official in-repository Cinema HUB OG logo is the only welcome image.
        # No random/external image fallback is allowed.
        self.start_image_urls = []
        self.telegram_read_timeout = int_env("TELEGRAM_READ_TIMEOUT", 45, 10, 180)
        self.telegram_connect_timeout = int_env("TELEGRAM_CONNECT_TIMEOUT", 30, 5, 120)
        self.telegram_write_timeout = int_env("TELEGRAM_WRITE_TIMEOUT", 45, 10, 180)
        self.telegram_pool_timeout = int_env("TELEGRAM_POOL_TIMEOUT", 30, 5, 120)
        self.startup_retry_delay = int_env("STARTUP_RETRY_DELAY", 5, 2, 60)
        # LOG_CHAT_ID is optional. Treat common dashboard placeholders such as
        # "Blank" as empty instead of crashing the whole bot at startup.
        self.log_chat_id = optional_int(
            env_first("LOG_CHAT_ID"), default=None, name="LOG_CHAT_ID"
        )

        self.expiry_notice = env_first(
            "EXPIRY_NOTICE",
            default=(
                "⚠️ ᴛʜɪꜱ ᴍᴏᴠɪᴇ ꜰɪʟᴇ/ᴠɪᴅᴇᴏ ᴡɪʟʟ ʙᴇ ᴅᴇʟᴇᴛᴇᴅ ɪɴ 5 ᴍɪɴᴜᴛᴇꜱ\n\n"
                "ᴘʟᴇᴀꜱᴇ ꜰᴏʀᴡᴀʀᴅ ᴛʜɪꜱ ꜰɪʟᴇ ᴛᴏ ꜱᴏᴍᴇᴡʜᴇʀᴇ ᴇʟꜱᴇ & "
                "ꜱᴛᴀʀᴛ ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴛʜᴇʀᴇ"
            ),
        ).replace("\\n", "\n")

        self._resolve_static_fsub()
        self._resolve_chat_ids()

    def _resolve_static_fsub(self):
        # F-Sub is optional. Do not make a malformed/old F-Sub setting prevent
        # the entire bot from starting. Actual chat accessibility and public
        # usernames are resolved against Telegram during Runtime validation.
        if self.fsub_links and not self.fsub_channels:
            log.warning(
                "FSUB_INVITE_LINKS is set but FSUB_CHANNELS is empty; "
                "disabling F-Sub until the configuration is corrected."
            )
            self.fsub_links = []
            self.require_fsub = False
            return
        if len(self.fsub_links) > len(self.fsub_channels):
            log.warning(
                "Ignoring %s extra FSUB_INVITE_LINKS entry/entries.",
                len(self.fsub_links) - len(self.fsub_channels),
            )
            self.fsub_links = self.fsub_links[: len(self.fsub_channels)]
        if not self.fsub_channels:
            self.require_fsub = False

    def _resolve_chat_ids(self):
        def normalize_chat(value):
            value = str(value).strip()
            if value.lstrip("-").isdigit():
                return int(value)
            return value
        self.database_channel = normalize_chat(self.database_channel)
        self.request_group = normalize_chat(self.request_group)
        self.fsub_channels = [normalize_chat(x) for x in self.fsub_channels]


# ---------------------------
# Text / metadata
# ---------------------------

QUALITY_RE = re.compile(
    r"(?i)\b(4k|2160p|1440p|2k|1080p|720p|480p|360p)\b"
)
SEASON_RE = re.compile(r"(?i)\b(?:season|s)\s*(\d{1,2})\b")
LANG_RE = re.compile(
    r"(?i)\b(hindi|english|tamil|telugu|malayalam|kannada|punjabi|bengali|dual audio|multi audio)\b"
)
SIZE_RE = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*(gb|mb)\b")
QUALITY_RANK = {
    "4K": 0,
    "2160P": 0,
    "1440P": 1,
    "2K": 1,
    "1080P": 2,
    "720P": 3,
    "480P": 4,
    "360P": 5,
}


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def parse_metadata(text):
    text = text or ""
    q = QUALITY_RE.search(text)
    s = SEASON_RE.search(text)
    l = LANG_RE.search(text)
    z = SIZE_RE.search(text)
    quality = q.group(1).upper() if q else None
    return {
        "quality": quality,
        "quality_rank": QUALITY_RANK.get(quality, 99),
        "season": f"Season {s.group(1)}" if s else None,
        "language": l.group(1).title() if l else None,
        "size": z.group(0).upper() if z else None,
    }


def media_filename(msg):
    for attr in ("video", "document", "audio"):
        item = getattr(msg, attr, None)
        if item and getattr(item, "file_name", None):
            return item.file_name
    file_obj = getattr(msg, "file", None)
    return getattr(file_obj, "name", None) if file_obj else None


def media_file_size(msg):
    """Return the Telegram media size in bytes when Telegram exposes it."""
    for attr in ("video", "document", "audio"):
        item = getattr(msg, attr, None)
        if item:
            size = getattr(item, "file_size", None)
            if size:
                try:
                    return int(size)
                except (TypeError, ValueError):
                    pass
    file_obj = getattr(msg, "file", None)
    size = getattr(file_obj, "size", None) if file_obj else None
    if size:
        try:
            return int(size)
        except (TypeError, ValueError):
            pass
    return None
# ---------------------------
# Search/index metadata helpers
# ---------------------------

SERIES_EPISODE_RE = re.compile(
    r"(?i)\b(?:s\s*\d{1,3}\s*e\s*\d{1,4}|season\s*\d{1,3}\s*(?:episode|ep|e)\s*\d{1,4}|(?:episode|ep|e)\s*\d{1,4})\b"
)
SEASON_EPISODE_PAIR_RE = re.compile(r"(?i)\bs\s*(\d{1,3})\s*e\s*(\d{1,4})\b")
SEASON_EPISODE_WORD_RE = re.compile(r"(?i)\bseason\s*(\d{1,3})\s*(?:episode|ep|e)\s*(\d{1,4})\b")
EPISODE_ONLY_RE = re.compile(r"(?i)\b(?:episode|ep|e)\s*(\d{1,4})\b")


def extract_episode_info(value):
    text = str(value or "")
    match = SEASON_EPISODE_PAIR_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = SEASON_EPISODE_WORD_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = EPISODE_ONLY_RE.search(text)
    if match:
        return None, int(match.group(1))
    return None, None


def derive_series_key(value):
    """Return a stable searchable series stem for episode-style titles."""
    text = normalize(value)
    if not text:
        return ""
    match = SERIES_EPISODE_RE.search(text)
    if match:
        prefix = text[:match.start()].strip(" _.-:|[]()")
        if prefix:
            return prefix
    # Common explicit season marker without an episode marker.
    season_marker = re.search(r"(?i)\b(?:season|s)\s*\d{1,3}\b", text)
    if season_marker:
        prefix = text[:season_marker.start()].strip(" _.-:|[]()")
        if prefix:
            return prefix
    return text


def message_caption_text(msg):
    return str(
        getattr(msg, "caption", None)
        or getattr(msg, "text", None)
        or getattr(msg, "message", None)
        or ""
    )


def message_id_value(msg):
    value = getattr(msg, "message_id", None)
    if value is None:
        value = getattr(msg, "id", None)
    return int(value) if value is not None else 0


def build_movie_document(msg, chat_id, caption_override=None, caption_html=None, caption_status="done"):
    """Build one canonical media record for both PTB and Telethon messages."""
    message_id = message_id_value(msg)
    source_caption = message_caption_text(msg)
    caption = str(caption_override if caption_override is not None else source_caption)
    filename = media_filename(msg)
    title = (
        caption.splitlines()[0].strip()
        if caption.strip()
        else (clean_result_name(filename) or f"File {message_id}")
    )
    meta = parse_metadata(f"{title}\n{caption}\n{filename or ''}")
    season_number, episode_number = extract_episode_info(f"{title} {filename or ''}")
    return {
        "chat_id": int(chat_id),
        "message_id": message_id,
        "title": title[:500],
        "caption": caption[:4000],
        "caption_html": str(caption_html or "")[:12000],
        "caption_status": caption_status,
        "filename": filename,
        "normalized": normalize(f"{title} {caption} {filename or ''}"),
        "series_key": derive_series_key(title or filename or caption),
        "quality": meta["quality"],
        "quality_rank": meta["quality_rank"],
        "language": meta["language"],
        "season": meta["season"],
        "season_number": season_number,
        "episode_number": episode_number,
        "size": meta["size"],
        "file_size": media_file_size(msg),
        "indexed_at": datetime.now(timezone.utc),
        "index_version": 2,
    }


def format_file_size(size_bytes):
    """Format a Telegram byte size exactly like the reference result list."""
    if size_bytes is None:
        return None
    try:
        size_bytes = float(size_bytes)
    except (TypeError, ValueError):
        return None
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    return f"{size_bytes / (1024 ** 2):.2f} MB"


def clean_result_name(value):
    """Make filenames readable without changing their actual identity."""
    text = str(value or "").strip()
    # The reference UI uses spaces instead of filename underscores.
    text = text.replace("_", " ")
    # Remove a leading source-tag such as [@TheEmpireBay] / [@FridayUniverse].
    text = re.sub(r"^\s*\[\s*@[^\]]+\]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def new_token(nbytes=24):
    return secrets.token_urlsafe(nbytes).replace("-", "_").replace("=", "")


def create_stream_token(cfg, user_id, movie_id):
    if not cfg.stream_signing_secret:
        raise RuntimeError("STREAM_SIGNING_SECRET is not configured.")
    payload = {
        "u": int(user_id),
        "m": str(movie_id),
        "e": int((datetime.now(timezone.utc) + timedelta(seconds=cfg.stream_token_ttl)).timestamp()),
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    sig = hmac.new(cfg.stream_signing_secret.encode(), raw.encode(), "sha256").hexdigest()
    return f"{raw}.{sig}"


# ---------------------------
# MongoDB
# ---------------------------

class Database:

    def __init__(self, uri, name):
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=15000)
        self.db = self.client[name]
        self.movies = self.db.movies
        self.tokens = self.db.tokens
        self.users = self.db.users
        self.searches = self.db.searches
        self.requests = self.db.requests
        self.fsub_gates = self.db.fsub_gates
        self.batches = self.db.batches
        self.search_counts = self.db.search_counts
        self.ingest_jobs = self.db.ingest_jobs
        self.premium_payment_requests = self.db.premium_payment_requests
        self.premium_actions = self.db.premium_actions
        self.verification_sessions = self.db.verification_sessions
        self.referrals = self.db.referrals
        self.index_state = self.db.index_state

        # Search caches are deliberately bounded and short-lived. A write invalidates
        # them so a newly indexed episode becomes searchable immediately.
        self._search_cache = {}
        self._search_cache_ttl = 3.0
        self._search_cache_lock = asyncio.Lock()
        self._search_db_semaphore = asyncio.Semaphore(
            max(2, min(int(os.getenv("SEARCH_DB_CONCURRENCY", "8") or "8"), 16))
        )

        # Fuzzy suggestions operate on distinct series stems, not individual episode rows.
        self._suggestion_cache = None
        self._suggestion_cache_at = 0.0
        self._suggestion_cache_ttl = 300.0
        self._suggestion_cache_lock = asyncio.Lock()

        self._search_tokens_ready = False


    async def init(self):
        last = None
        for attempt in range(1, 7):
            try:
                await self.client.admin.command("ping")
                await self._migrate_users()
                await self.movies.create_index(
                    [("chat_id", ASCENDING), ("message_id", ASCENDING)], unique=True
                )
                await self.movies.create_index([("normalized", ASCENDING)])
                await self.movies.create_index([("search_tokens", ASCENDING)])
                await self.movies.create_index([("series_key", ASCENDING)])
                await self.movies.create_index([("series_key", ASCENDING), ("quality_rank", ASCENDING), ("season_number", ASCENDING), ("episode_number", ASCENDING), ("message_id", ASCENDING)])
                await self.movies.create_index([("quality_rank", ASCENDING), ("season_number", ASCENDING), ("episode_number", ASCENDING), ("message_id", ASCENDING)])
                await self.movies.create_index([("title", ASCENDING)])
                await self.movies.create_index([("quality", ASCENDING)])
                await self.movies.create_index([("language", ASCENDING)])
                await self.movies.create_index([("season", ASCENDING)])
                await self.movies.create_index([("caption_status", ASCENDING), ("caption_next_attempt_at", ASCENDING)])
                await self.tokens.create_index("token", unique=True)
                await self.tokens.create_index("expires_at", expireAfterSeconds=0)
                await self.users.create_index("user_id", unique=True, sparse=True)
                await self.searches.create_index("expires_at", expireAfterSeconds=0)
                await self.searches.create_index([("result_expires_at", ASCENDING), ("result_deleted_at", ASCENDING)])
                await self.searches.create_index([("request_expires_at", ASCENDING), ("request_deleted_at", ASCENDING)])
                await self.requests.create_index("expires_at", expireAfterSeconds=0)
                await self.fsub_gates.create_index("gate_id", unique=True)
                await self.fsub_gates.create_index("expires_at", expireAfterSeconds=0)
                await self.batches.create_index("batch_id", unique=True)
                await self.batches.create_index("expires_at", expireAfterSeconds=0)
                await self.search_counts.create_index("normalized", unique=True)
                await self.search_counts.create_index([("count", ASCENDING)])
                await self.ingest_jobs.create_index([("source", ASCENDING), ("identifier", ASCENDING)], unique=True)
                await self.ingest_jobs.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
                await self.premium_payment_requests.create_index("request_id", unique=True)
                await self.premium_payment_requests.create_index([("user_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
                await self.premium_actions.create_index("token", unique=True)
                await self.premium_actions.create_index("expires_at", expireAfterSeconds=0)
                await self.verification_sessions.create_index("session_id", unique=True)
                await self.verification_sessions.create_index("expires_at", expireAfterSeconds=0)
                await self.verification_sessions.create_index([("user_id", ASCENDING), ("verified_until", ASCENDING)])
                await self.referrals.create_index("referred_user_id", unique=True)
                await self.referrals.create_index([("referrer_id", ASCENDING), ("status", ASCENDING)])
                await self.index_state.create_index("name", unique=True)
                return
            except Exception as exc:
                last = exc
                log.exception("MongoDB initialization attempt %s/6 failed", attempt)
                if attempt < 6:
                    await asyncio.sleep(min(2 ** (attempt - 1), 20))
        raise RuntimeError(f"MongoDB connection failed: {last}")

    async def _migrate_users(self):
        # Older AutoFilter databases commonly used `id` instead of `user_id`,
        # or stored user_id as a string. Normalize that schema before creating
        # the unique index so deployment never dies on duplicate/null user_id.
        # index_information() is a direct async method returning a mapping;
        # using it here avoids version-specific AsyncCommandCursor/to_list
        # handling during startup.
        index_info = await self.users.index_information()
        for index_name, info in list(index_info.items()):
            if index_name == "_id_":
                continue
            try:
                key = list(info.get("key", []))
            except Exception:
                key = []
            if key == [("user_id", 1)]:
                try:
                    await self.users.drop_index(index_name)
                except Exception:
                    log.exception("Could not drop legacy user_id index %s", index_name)

        docs = await self.users.find(
            {}, {"_id": 1, "id": 1, "user_id": 1}
        ).to_list(length=None)
        seen = {}
        for doc in docs:
            raw = doc.get("user_id")
            if raw is None or str(raw).strip() == "" or is_placeholder(raw):
                raw = doc.get("id")
            uid = None
            if raw is not None:
                try:
                    uid = int(raw)
                except (TypeError, ValueError):
                    uid = None

            # If a malformed user_id exists but the legacy numeric `id` is
            # usable, recover from the legacy field instead of discarding the
            # user record.
            if uid is None and raw != doc.get("id"):
                legacy_id = doc.get("id")
                try:
                    uid = int(legacy_id) if legacy_id is not None else None
                except (TypeError, ValueError):
                    uid = None

            if uid is None:
                await self.users.update_one(
                    {"_id": doc["_id"]}, {"$unset": {"user_id": ""}}
                )
                continue

            if uid in seen:
                # Preserve one canonical record per Telegram user. If an old
                # migration created duplicates, keep the first document.
                await self.users.delete_one({"_id": doc["_id"]})
                continue

            seen[uid] = doc["_id"]
            await self.users.update_one(
                {"_id": doc["_id"]}, {"$set": {"user_id": uid}}
            )
    async def close(self):
        await self.client.close()


    async def upsert_movie(self, doc):
        doc = dict(doc)
        normalized = normalize(str(doc.get("normalized") or ""))
        if normalized:
            doc["normalized"] = normalized
            doc["search_tokens"] = sorted(set(normalized.split()))
        if not doc.get("series_key"):
            doc["series_key"] = derive_series_key(doc.get("title") or doc.get("filename") or "")
        season_number, episode_number = extract_episode_info(
            f"{doc.get('title') or ''} {doc.get('filename') or ''}"
        )
        doc["season_number"] = season_number
        doc["episode_number"] = episode_number
        doc.setdefault("index_version", 2)
        await self.movies.update_one(
            {"chat_id": int(doc["chat_id"]), "message_id": int(doc["message_id"])},
            {"$set": doc},
            upsert=True,
        )
        await self.invalidate_search_cache()

    async def bulk_upsert_movies(self, docs):
        docs = [dict(doc) for doc in docs if doc]
        if not docs:
            return 0
        ops = []
        for raw in docs:
            doc = dict(raw)
            normalized = normalize(str(doc.get("normalized") or ""))
            if normalized:
                doc["normalized"] = normalized
                doc["search_tokens"] = sorted(set(normalized.split()))
            if not doc.get("series_key"):
                doc["series_key"] = derive_series_key(doc.get("title") or doc.get("filename") or "")
            season_number, episode_number = extract_episode_info(
                f"{doc.get('title') or ''} {doc.get('filename') or ''}"
            )
            doc["season_number"] = season_number
            doc["episode_number"] = episode_number
            doc.setdefault("index_version", 2)
            ops.append(
                UpdateOne(
                    {"chat_id": int(doc["chat_id"]), "message_id": int(doc["message_id"])},
                    {"$set": doc},
                    upsert=True,
                )
            )
        result = await self.movies.bulk_write(ops, ordered=False)
        await self.invalidate_search_cache()
        return int(result.upserted_count + result.modified_count)


    async def invalidate_search_cache(self):
        async with self._search_cache_lock:
            self._search_cache.clear()
        async with self._suggestion_cache_lock:
            self._suggestion_cache = None
            self._suggestion_cache_at = 0.0


    def _apply_filters(self, criteria, filters):
        filters = filters or {}
        criteria = dict(criteria)
        for key in ("quality", "language", "season"):
            if filters.get(key):
                criteria[key] = filters[key]
        return criteria

    def _exact_search_criteria(self, query, filters=None):
        words = [w for w in normalize(query).split() if len(w) >= 2]
        if not words:
            return None
        if self._search_tokens_ready:
            return self._apply_filters({"search_tokens": {"$all": words}}, filters)
        return self._apply_filters(
            {"$and": [{"normalized": {"$regex": re.escape(word), "$options": "i"}} for word in words]},
            filters,
        )

    def _series_regex_criteria(self, series_key, filters=None):
        words = [w for w in normalize(series_key).split() if len(w) >= 2]
        if not words:
            return None
        return self._apply_filters(
            {"$and": [{"normalized": {"$regex": re.escape(word), "$options": "i"}} for word in words]},
            filters,
        )


    def _regex_search_criteria(self, query, filters=None):
        return self._exact_search_criteria(query, filters)

    async def _resolve_fuzzy_series_keys(self, query, limit=3):
        q = normalize(query)
        if not q:
            return []
        candidates = await self._get_title_candidates()
        scored = []
        for series_key in candidates:
            score = self._fuzzy_title_score(q, series_key)
            if score >= 0.68:
                scored.append((score, series_key))
        scored.sort(key=lambda item: (-item[0], item[1].lower()))
        return scored[: max(1, limit)]

    async def _search_cache_get(self, key):
        now = time.monotonic()
        async with self._search_cache_lock:
            item = self._search_cache.get(key)
            if not item:
                return None
            created, value = item
            if now - created > self._search_cache_ttl:
                self._search_cache.pop(key, None)
                return None
            return value

    async def _search_cache_set(self, key, value):
        async with self._search_cache_lock:
            self._search_cache[key] = (time.monotonic(), value)
            if len(self._search_cache) > 512:
                oldest = sorted(self._search_cache.items(), key=lambda item: item[1][0])[:64]
                for old_key, _ in oldest:
                    self._search_cache.pop(old_key, None)


    async def _fast_title_matches(self, query, limit=24):
        matches = await self._resolve_fuzzy_series_keys(query, limit=limit)
        return [title for _, title in matches]


    async def _resolved_search_criteria(self, query, filters=None):
        criteria = self._exact_search_criteria(query, filters)
        if criteria is None:
            return None, False
        return criteria, False


    async def backfill_search_tokens(self, batch_size=500):
        """Backfill the v2 search metadata without blocking startup."""
        last_log = time.monotonic()
        total = 0
        try:
            while True:
                docs = await self.movies.find(
                    {
                        "$or": [
                            {"search_tokens": {"$exists": False}},
                            {"series_key": {"$exists": False}},
                            {"index_version": {"$ne": 2}},
                        ]
                    },
                    {
                        "_id": 1,
                        "title": 1,
                        "caption": 1,
                        "filename": 1,
                        "normalized": 1,
                    },
                ).limit(batch_size).to_list(length=batch_size)
                if not docs:
                    self._search_tokens_ready = True
                    log.info("Search metadata backfill complete: %s records updated.", total)
                    return total
                updates = []
                for row in docs:
                    normalized = normalize(
                        f"{row.get('title') or ''} {row.get('caption') or ''} {row.get('filename') or ''}"
                    )
                    title = str(row.get("title") or row.get("filename") or "")
                    season_number, episode_number = extract_episode_info(
                        f"{title} {row.get('filename') or ''}"
                    )
                    updates.append(
                        UpdateOne(
                            {"_id": row["_id"]},
                            {"$set": {
                                "normalized": normalized,
                                "search_tokens": sorted(set(normalized.split())),
                                "series_key": derive_series_key(title),
                                "season_number": season_number,
                                "episode_number": episode_number,
                                "index_version": 2,
                            }},
                        )
                    )
                if updates:
                    result = await self.movies.bulk_write(updates, ordered=False)
                    total += int(result.modified_count)
                await self.invalidate_search_cache()
                if time.monotonic() - last_log >= 10:
                    remaining = await self.movies.count_documents({"index_version": {"$ne": 2}})
                    log.info("Search metadata backfill in progress; about %s records remain.", remaining)
                    last_log = time.monotonic()
                await asyncio.sleep(0)
        except Exception:
            log.exception("Search metadata backfill failed; regex fallback remains active.")
            self._search_tokens_ready = False
            return total

    @staticmethod
    def _criteria_key(query, filters):
        return json.dumps({"q": normalize(query), "f": filters or {}}, sort_keys=True, separators=(",", ":"))


    async def count_movies(self, query, filters=None):
        key = "count:" + self._criteria_key(query, filters)
        cached = await self._search_cache_get(key)
        if cached is not None:
            return int(cached)
        async with self._search_db_semaphore:
            criteria = self._exact_search_criteria(query, filters)
            if criteria is None:
                return 0
            count = int(await self.movies.count_documents(criteria))
            if count == 0:
                matches = await self._resolve_fuzzy_series_keys(query, limit=3)
                if matches:
                    keys = [value for _, value in matches]
                    series_criteria = self._apply_filters({"series_key": {"$in": keys}}, filters)
                    count = int(await self.movies.count_documents(series_criteria))
                    if count == 0:
                        # Compatibility path for records that have not yet received v2 metadata.
                        or_criteria = {"$or": []}
                        for key_value in keys:
                            key_criteria = self._series_regex_criteria(key_value, None)
                            if key_criteria:
                                or_criteria["$or"].append(key_criteria)
                        if or_criteria["$or"]:
                            or_criteria = self._apply_filters(or_criteria, filters)
                            count = int(await self.movies.count_documents(or_criteria))
        await self._search_cache_set(key, count)
        return count


    async def find_movies(self, query, limit, filters=None, skip=0):
        key = "find:" + self._criteria_key(query, filters) + f":{int(limit)}:{int(skip)}"
        cached = await self._search_cache_get(key)
        if cached is not None:
            return list(cached)
        async with self._search_db_semaphore:
            criteria = self._exact_search_criteria(query, filters)
            if criteria is None:
                return []
            limit = max(1, int(limit))
            skip = max(0, int(skip))
            query_count = int(await self.movies.count_documents(criteria))
            if query_count == 0:
                matches = await self._resolve_fuzzy_series_keys(query, limit=3)
                if matches:
                    keys = [value for _, value in matches]
                    criteria = self._apply_filters({"series_key": {"$in": keys}}, filters)
                    query_count = int(await self.movies.count_documents(criteria))
                    if query_count == 0:
                        or_parts = []
                        for key_value in keys:
                            key_criteria = self._series_regex_criteria(key_value, None)
                            if key_criteria:
                                or_parts.append(key_criteria)
                        if or_parts:
                            criteria = self._apply_filters({"$or": or_parts}, filters)
            cursor = (
                self.movies.find(criteria)
                .sort([
                    ("quality_rank", ASCENDING),
                    ("season_number", ASCENDING),
                    ("episode_number", ASCENDING),
                    ("title", ASCENDING),
                    ("message_id", ASCENDING),
                ])
                .skip(skip)
                .limit(limit)
            )
            results = await cursor.to_list(length=limit)
        await self._search_cache_set(key, tuple(results))
        return results


    async def distinct_movie_values(self, query, field, filters=None):
        if field not in {"quality", "language", "season"}:
            return []
        cache_key = "distinct:" + field + ":" + self._criteria_key(query, filters)
        cached = await self._search_cache_get(cache_key)
        if cached is not None:
            return list(cached)
        async with self._search_db_semaphore:
            criteria = self._exact_search_criteria(query, filters)
            if criteria is None:
                return []
            values = await self.movies.distinct(field, criteria)
            if not values:
                matches = await self._resolve_fuzzy_series_keys(query, limit=3)
                if matches:
                    keys = [value for _, value in matches]
                    values = await self.movies.distinct(
                        field, self._apply_filters({"series_key": {"$in": keys}}, filters)
                    )
        out = sorted({str(value).strip() for value in values if str(value).strip()}, key=str.lower)
        await self._search_cache_set(cache_key, tuple(out))
        return out

    @staticmethod
    def _fuzzy_title_score(query, title):
        """Score a possible title correction without adding a heavy dependency.

        The score combines whole-string similarity, order-insensitive similarity,
        and per-word similarity. This catches practical typos such as:
        - Kabri Singh -> Kabir Singh
        - Kakli -> Kalki
        - Conjring -> The Conjuring
        - Avatr -> Avatar
        """
        q = normalize(query)
        t = normalize(title)
        if not q or not t:
            return 0.0

        direct = SequenceMatcher(None, q, t).ratio()
        sorted_ratio = SequenceMatcher(
            None, " ".join(sorted(q.split())), " ".join(sorted(t.split()))
        ).ratio()

        q_words = q.split()
        t_words = t.split()
        if q_words and t_words:
            best_word_scores = [
                max(SequenceMatcher(None, qw, tw).ratio() for tw in t_words)
                for qw in q_words
            ]
            word_score = sum(best_word_scores) / len(best_word_scores)
            word_coverage = sum(1 for score in best_word_scores if score >= 0.70) / len(best_word_scores)
        else:
            word_score = 0.0
            word_coverage = 0.0

        # Small bonuses for useful structural matches.
        prefix_bonus = 0.05 if t.startswith(q[: min(4, len(q))]) else 0.0
        contains_bonus = 0.04 if q in t or t in q else 0.0

        return min(1.0, (
            direct * 0.42
            + sorted_ratio * 0.23
            + word_score * 0.25
            + word_coverage * 0.10
            + prefix_bonus
            + contains_bonus
        ))


    async def _get_title_candidates(self):
        now = time.monotonic()
        if self._suggestion_cache is not None and now - self._suggestion_cache_at < self._suggestion_cache_ttl:
            return self._suggestion_cache
        async with self._suggestion_cache_lock:
            now = time.monotonic()
            if self._suggestion_cache is not None and now - self._suggestion_cache_at < self._suggestion_cache_ttl:
                return self._suggestion_cache
            try:
                series_values = await self.movies.distinct(
                    "series_key", {"series_key": {"$type": "string", "$ne": ""}}
                )
                title_values = await self.movies.distinct(
                    "title", {"title": {"$type": "string", "$ne": ""}}
                )
                cleaned = []
                seen = set()
                for raw in [*series_values, *title_values]:
                    value = derive_series_key(raw)
                    if not value:
                        continue
                    if value in seen:
                        continue
                    seen.add(value)
                    cleaned.append(value)
                self._suggestion_cache = cleaned
                self._suggestion_cache_at = now
            except Exception:
                log.exception("Could not collect fuzzy-search candidates")
                return self._suggestion_cache or []
        return self._suggestion_cache or []


    async def suggestion_matches(self, query, limit=5):
        q = normalize(query)
        if not q:
            return []
        titles = await self._get_title_candidates()
        scored = []
        for title in titles:
            score = self._fuzzy_title_score(q, title)
            if score >= 0.60:
                scored.append((score, title))
        scored.sort(key=lambda item: (-item[0], item[1].lower()))
        return scored[: max(1, limit)]

    async def suggestions(self, query, limit=5):
        return [title for _, title in await self.suggestion_matches(query, limit)]

    async def record_user(self, user_id, **extra):
        user_id = int(user_id)
        now = datetime.now(timezone.utc)
        result = await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {"last_seen": now, **extra},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return result.upserted_id is not None

    async def register_referral(self, referrer_id, referred_user_id, is_new_user):
        """Record only genuine first-time bot users, preventing self/duplicate referrals."""
        try:
            referrer_id, referred_user_id = int(referrer_id), int(referred_user_id)
        except (TypeError, ValueError):
            return False
        if not is_new_user or referrer_id <= 0 or referred_user_id <= 0 or referrer_id == referred_user_id:
            return False
        if not await self.users.find_one({"user_id": referrer_id}, {"user_id": 1}):
            return False
        now = datetime.now(timezone.utc)
        result = await self.referrals.update_one(
            {"referred_user_id": referred_user_id},
            {"$setOnInsert": {
                "referrer_id": referrer_id,
                "referred_user_id": referred_user_id,
                "status": "pending",
                "created_at": now,
            }},
            upsert=True,
        )
        return result.upserted_id is not None

    async def referral_summary(self, referrer_id):
        referrer_id = int(referrer_id)
        total = await self.referrals.count_documents({"referrer_id": referrer_id, "status": "qualified"})
        pending = await self.referrals.count_documents({"referrer_id": referrer_id, "status": {"$in": ["pending", "rewarding"]}})
        return {"qualified": total, "pending": pending}

    async def qualify_referral_for_user(self, referred_user_id):
        """Credit one Premium day once, atomically per referred user ID."""
        referred_user_id = int(referred_user_id)
        now = datetime.now(timezone.utc)
        # Recover a worker interrupted mid-reward. The user's ledger below prevents
        # a repeated Premium extension if the credit had already reached MongoDB.
        await self.referrals.update_many(
            {"status": "rewarding", "claimed_at": {"$lt": now - timedelta(minutes=5)}},
            {"$set": {"status": "pending"}},
        )
        referral = await self.referrals.find_one_and_update(
            {"referred_user_id": referred_user_id, "status": "pending"},
            {"$set": {"status": "rewarding", "claimed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not referral:
            return None
        referrer_id = int(referral.get("referrer_id") or 0)
        if not referrer_id or referrer_id == referred_user_id:
            await self.referrals.update_one(
                {"referred_user_id": referred_user_id},
                {"$set": {"status": "rejected", "reason": "invalid_referrer"}},
            )
            return None

        # Update pipeline extends any still-active plan by exactly 24 hours. The
        # referral_reward_ids ledger makes the credit idempotent across retries.
        credited = await self.users.find_one_and_update(
            {"user_id": referrer_id, "referral_reward_ids": {"$ne": referred_user_id}},
            [{"$set": {
                "premium_until": {"$add": [
                    {"$cond": [
                        {"$gt": [{"$ifNull": ["$premium_until", now]}, now]},
                        "$premium_until",
                        now,
                    ]},
                    86400000,
                ]},
                "referral_reward_ids": {"$concatArrays": [
                    {"$ifNull": ["$referral_reward_ids", []]}, [referred_user_id]
                ]},
                "premium_active": True,
                "premium_plan": {"$ifNull": ["$premium_plan", "referral"]},
                "premium_last_price": {"$ifNull": ["$premium_last_price", 0]},
                "premium_updated_at": now,
                "premium_source": "referral",
            }}],
            return_document=ReturnDocument.AFTER,
        )
        if credited is None:
            existing = await self.users.find_one({"user_id": referrer_id}, {"premium_until": 1, "referral_reward_ids": 1})
            if not existing or referred_user_id not in (existing.get("referral_reward_ids") or []):
                await self.referrals.update_one(
                    {"referred_user_id": referred_user_id},
                    {"$set": {"status": "pending"}, "$unset": {"claimed_at": ""}},
                )
                return None
            credited = existing  # Already credited; don't extend it again.

        await self.referrals.update_one(
            {"referred_user_id": referred_user_id},
            {"$set": {
                "status": "qualified",
                "qualified_at": now,
                "reward_days": 1,
                "rewarded_at": now,
            }},
        )
        return {
            "referrer_id": referrer_id,
            "premium_until": credited.get("premium_until"),
            "referred_user_id": referred_user_id,
        }

    async def record_request(self, user_id, query):
        user_id = int(user_id) if user_id is not None else 0
        now = datetime.now(timezone.utc)
        await self.requests.insert_one({
            "user_id": user_id,
            "query": query,
            "created_at": now,
            "expires_at": now + timedelta(days=30),
        })
        normalized_query = normalize(query)
        if normalized_query:
            await self.search_counts.update_one(
                {"normalized": normalized_query},
                {"$set": {"query": query.strip()[:200], "updated_at": now}, "$inc": {"count": 1}, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )

    async def top_searches(self, limit=10):
        cursor = self.search_counts.find({}, {"_id": 0, "query": 1, "count": 1}).sort([("count", -1), ("query", 1)]).limit(limit)
        return await cursor.to_list(length=limit)

    async def create_search(
        self,
        user_id,
        query,
        filters,
        expires_at,
        request_chat_id=None,
        request_message_id=None,
    ):
        sid = new_token(10)
        doc = {
            "search_id": sid,
            "user_id": user_id,
            "query": query,
            "filters": filters or {},
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
        }
        if request_chat_id is not None and request_message_id is not None:
            doc["request_chat_id"] = int(request_chat_id)
            doc["request_message_id"] = int(request_message_id)
        await self.searches.insert_one(doc)
        return sid

    async def get_search(self, sid, user_id):
        return await self.searches.find_one(
            {"search_id": sid, "user_id": user_id}
        )

    async def update_search_filter(self, sid, user_id, filters):
        await self.searches.update_one(
            {"search_id": sid, "user_id": user_id},
            {"$set": {"filters": filters}},
        )

    async def set_search_result_message_once(
        self, sid, user_id, chat_id, message_id, expires_at
    ):
        """Persist the first result message and one shared 10-minute expiry.

        The same fixed expiry is used for the user's original request message,
        so pagination/filter edits never extend the lifetime.
        """
        return await self.searches.update_one(
            {
                "search_id": sid,
                "user_id": user_id,
                "result_message_id": {"$exists": False},
            },
            {
                "$set": {
                    "result_chat_id": int(chat_id),
                    "result_message_id": int(message_id),
                    "result_expires_at": expires_at,
                    "request_expires_at": expires_at,
                }
            },
        )

    async def mark_search_result_deleted(self, sid):
        await self.searches.update_one(
            {"search_id": sid},
            {"$set": {"result_deleted_at": datetime.now(timezone.utc)}},
        )

    async def mark_search_request_deleted(self, sid):
        await self.searches.update_one(
            {"search_id": sid},
            {"$set": {"request_deleted_at": datetime.now(timezone.utc)}},
        )

    async def expired_search_messages(self, limit=100):
        now = datetime.now(timezone.utc)
        cursor = (
            self.searches.find(
                {
                    "$or": [
                        {
                            "result_message_id": {"$exists": True},
                            "result_deleted_at": {"$exists": False},
                            "result_expires_at": {"$lte": now},
                        },
                        {
                            "request_message_id": {"$exists": True},
                            "request_deleted_at": {"$exists": False},
                            "request_expires_at": {"$lte": now},
                        },
                    ]
                },
                {
                    "search_id": 1,
                    "request_chat_id": 1,
                    "request_message_id": 1,
                    "request_expires_at": 1,
                    "request_deleted_at": 1,
                    "result_chat_id": 1,
                    "result_message_id": 1,
                    "result_expires_at": 1,
                    "result_deleted_at": 1,
                },
            )
            .sort("result_expires_at", ASCENDING)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def create_fsub_gate(self, gate_id, user_id, movie_ids, expires_at, context=None):
        doc = {
            "gate_id": gate_id,
            "user_id": user_id,
            "movie_ids": movie_ids,
            "created_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "used": False,
        }
        if context is not None:
            doc["context"] = context
        await self.fsub_gates.insert_one(doc)

    async def consume_fsub_gate(self, gate_id, user_id):
        return await self.fsub_gates.find_one_and_update(
            {
                "gate_id": gate_id,
                "user_id": user_id,
                "used": False,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def create_token(self, token, user_id, movie_ids, expires_at):
        await self.tokens.insert_one(
            {
                "token": token,
                "user_id": user_id,
                "movie_ids": movie_ids,
                "created_at": datetime.now(timezone.utc),
                "expires_at": expires_at,
                "used": False,
            }
        )

    async def consume_token(self, token, user_id):
        return await self.tokens.find_one_and_update(
            {
                "token": token,
                "user_id": user_id,
                "used": False,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def create_batch(self, batch_id, user_id, movie_ids, expires_at):
        await self.batches.insert_one(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "movie_ids": movie_ids,
                "created_at": datetime.now(timezone.utc),
                "expires_at": expires_at,
                "used": False,
            }
        )

    async def consume_batch(self, batch_id, user_id):
        return await self.batches.find_one_and_update(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "used": False,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def get_user(self, user_id):
        return await self.users.find_one({"user_id": int(user_id)})

    async def is_premium(self, user_id):
        doc = await self.get_user(user_id)
        until = (doc or {}).get("premium_until")
        if not until:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)

    async def activate_premium(self, user_id, days, plan_id, price, source="admin"):
        user_id = int(user_id)
        now = datetime.now(timezone.utc)
        current = await self.users.find_one({"user_id": user_id}, {"premium_until": 1})
        current_until = (current or {}).get("premium_until")
        if current_until and current_until.tzinfo is None:
            current_until = current_until.replace(tzinfo=timezone.utc)
        base = current_until if current_until and current_until > now else now
        new_until = base + timedelta(days=int(days))
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {
                "premium_until": new_until,
                "premium_active": True,
                "premium_plan": plan_id,
                "premium_last_price": int(price),
                "premium_updated_at": now,
                "premium_source": source,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return new_until

    async def remove_premium(self, user_id):
        await self.users.update_one(
            {"user_id": int(user_id)},
            {"$set": {"premium_active": False, "premium_until": datetime.now(timezone.utc)}}
        )

    async def create_premium_payment_request(self, user_id, plan_id):
        rid = new_token(10)
        plan = PREMIUM_PLANS[plan_id]
        now = datetime.now(timezone.utc)
        await self.premium_payment_requests.insert_one({
            "request_id": rid,
            "user_id": int(user_id),
            "plan_id": plan_id,
            "status": "pending",
            "price": plan["price"],
            "days": plan["days"],
            "created_at": now,
            "updated_at": now,
        })
        return rid

    async def get_premium_payment_request(self, request_id):
        return await self.premium_payment_requests.find_one({"request_id": str(request_id)})

    async def latest_pending_payment_request(self, user_id):
        return await self.premium_payment_requests.find_one(
            {"user_id": int(user_id), "status": "pending"},
            sort=[("created_at", -1)],
        )

    async def touch_payment_request(self, request_id, user_id):
        await self.premium_payment_requests.update_one(
            {"request_id": str(request_id), "user_id": int(user_id)},
            {"$set": {"payment_bot_started_at": datetime.now(timezone.utc)}}
        )

    async def attach_payment_screenshot(self, request_id, user_id, file_id, caption=""):
        await self.premium_payment_requests.update_one(
            {"request_id": str(request_id), "user_id": int(user_id), "status": "pending"},
            {"$set": {
                "screenshot_file_id": file_id,
                "screenshot_caption": caption[:1000],
                "screenshot_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }}
        )

    async def claim_payment_request(self, request_id, admin_id, action):
        """Atomically claim a pending payment request for one admin action."""
        now = datetime.now(timezone.utc)
        processing_status = f"{action}_processing"
        stale_before = now - timedelta(minutes=10)
        return await self.premium_payment_requests.find_one_and_update(
            {
                "request_id": str(request_id),
                "$or": [
                    {"status": "pending"},
                    {"status": processing_status, "processing_at": {"$lt": stale_before}},
                ],
            },
            {"$set": {
                "status": processing_status,
                "admin_id": int(admin_id),
                "processing_at": now,
                "updated_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )

    async def finalize_payment_request(self, request_id, status, admin_id):
        now = datetime.now(timezone.utc)
        return await self.premium_payment_requests.find_one_and_update(
            {"request_id": str(request_id)},
            {"$set": {
                "status": status,
                "admin_id": int(admin_id),
                "handled_at": now,
                "updated_at": now,
            }, "$unset": {"processing_at": ""}},
            return_document=ReturnDocument.AFTER,
        )

    async def mark_payment_request(self, request_id, status, admin_id):
        # Backward-compatible helper for older manual/admin flows.
        return await self.finalize_payment_request(request_id, status, admin_id)

    async def create_verification_session(self, user_id, movie_ids, expires_at):
        # The stage is separated with an underscore in the deep-link payload;
        # keep this ID strictly alphanumeric and parse with rsplit for old links.
        sid = secrets.token_urlsafe(12).replace("_", "").replace("-", "")
        now = datetime.now(timezone.utc)
        await self.verification_sessions.insert_one({
            "session_id": sid,
            "user_id": int(user_id),
            "movie_ids": [str(x) for x in movie_ids],
            "softurl_verified": False,
            "created_at": now,
            "expires_at": expires_at,
        })
        return sid

    async def get_verification_session(self, session_id, user_id):
        return await self.verification_sessions.find_one({
            "session_id": str(session_id),
            "user_id": int(user_id),
            "expires_at": {"$gt": datetime.now(timezone.utc)},
        })

    async def mark_verification_stage(self, session_id, user_id, stage):
        # Only the Softurl callback is a valid verification completion stage.
        if stage != "soft":
            return None
        return await self.verification_sessions.find_one_and_update(
            {
                "session_id": str(session_id),
                "user_id": int(user_id),
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"softurl_verified": True, "last_verified_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def set_verification_access(self, user_id, until):
        await self.users.update_one(
            {"user_id": int(user_id)},
            {"$set": {"verification_until": until}},
            upsert=True,
        )

    async def has_verification_access(self, user_id):
        doc = await self.users.find_one({"user_id": int(user_id)}, {"verification_until": 1})
        until = (doc or {}).get("verification_until")
        if not until:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)

    async def create_premium_action(self, user_id, movie_ids, action_type="skip_verification", ttl=86400):
        token = new_token(12)
        await self.premium_actions.insert_one({
            "token": token,
            "user_id": int(user_id),
            "movie_ids": [str(x) for x in movie_ids],
            "action_type": action_type,
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
            "used": False,
        })
        return token

    async def get_premium_action(self, token, user_id):
        return await self.premium_actions.find_one({
            "token": str(token), "user_id": int(user_id), "used": False,
            "expires_at": {"$gt": datetime.now(timezone.utc)},
        })

    async def consume_premium_action(self, token, user_id):
        return await self.premium_actions.find_one_and_update(
            {"token": str(token), "user_id": int(user_id), "used": False, "expires_at": {"$gt": datetime.now(timezone.utc)}},
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def premium_list(self, limit=50):
        return await self.users.find(
            {"premium_until": {"$gt": datetime.now(timezone.utc)}},
            {"user_id": 1, "premium_until": 1, "premium_plan": 1},
        ).sort("premium_until", ASCENDING).limit(limit).to_list(length=limit)


    async def get_index_state(self, name="historical"):
        return await self.index_state.find_one({"name": name})

    async def set_index_state(self, name, **fields):
        fields["updated_at"] = datetime.now(timezone.utc)
        await self.index_state.update_one(
            {"name": name}, {"$set": fields, "$setOnInsert": {"name": name}}, upsert=True
        )

    async def get_pending_captions(self, limit=10):
        now = datetime.now(timezone.utc)
        cursor = (
            self.movies.find(
                {
                    "caption_status": "pending",
                    "caption_html": {"$type": "string", "$ne": ""},
                    "$or": [
                        {"caption_next_attempt_at": {"$exists": False}},
                        {"caption_next_attempt_at": {"$lte": now}},
                    ],
                },
                {"chat_id": 1, "message_id": 1, "caption_html": 1, "caption_attempts": 1},
            )
            .sort("indexed_at", ASCENDING)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def mark_caption_pending(self, chat_id, message_id, attempts=0, error=""):
        now = datetime.now(timezone.utc)
        await self.movies.update_one(
            {"chat_id": int(chat_id), "message_id": int(message_id)},
            {"$set": {
                "caption_status": "pending",
                "caption_attempts": int(attempts),
                "caption_last_error": str(error or "")[:500],
                "caption_next_attempt_at": now + timedelta(seconds=min(900, max(5, 2 ** min(int(attempts), 8)))),
            }},
        )

    async def mark_caption_done(self, chat_id, message_id):
        await self.movies.update_one(
            {"chat_id": int(chat_id), "message_id": int(message_id)},
            {"$set": {
                "caption_status": "done",
                "caption_formatted_at": datetime.now(timezone.utc),
                "caption_last_error": "",
            }, "$unset": {"caption_next_attempt_at": ""}},
        )

    async def stats(self):
        state = await self.get_index_state("historical") or {}
        return {
            "movies": await self.movies.count_documents({}),
            "users": await self.users.count_documents({}),
            "tokens": await self.tokens.count_documents({}),
            "requests": await self.requests.count_documents({}),
            "ingest_jobs": await self.ingest_jobs.count_documents({}),
            "ingest_uploaded": await self.ingest_jobs.count_documents({"status": "uploaded"}),
            "caption_pending": await self.movies.count_documents({"caption_status": "pending"}),
            "caption_failed": await self.movies.count_documents({"caption_status": "failed"}),
            "historical_status": state.get("status", "never"),
            "historical_last_message_id": state.get("last_message_id"),
            "historical_last_count": state.get("indexed", 0),
            "historical_updated_at": state.get("updated_at"),
        }


# ---------------------------
# Shorteners / verification
# ---------------------------

def _redact_shortener_detail(value, api_token="", destination=""):
    text = str(value or "")
    for secret in (api_token, quote(api_token, safe="") if api_token else "", destination):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text[:300]


async def _shorten_http(cfg, base_url, api_token, destination, label):
    if not api_token:
        raise RuntimeError(f"{label}: API token is missing")
    timeout = httpx.Timeout(20.0, connect=10.0)
    last_detail = "unknown error"
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.get(
                    base_url,
                    params={"api": api_token, "url": destination},
                )
                raw = response.text.strip()
                if not response.is_success:
                    last_detail = f"HTTP {response.status_code}: {_redact_shortener_detail(raw, api_token, destination)}"
                    raise RuntimeError(last_detail)

                content_type = (response.headers.get("content-type") or "").lower()
                if "json" in content_type or raw.startswith("{"):
                    try:
                        data = response.json()
                    except ValueError as exc:
                        last_detail = f"HTTP {response.status_code}: invalid JSON response"
                        raise RuntimeError(last_detail) from exc
                    if str(data.get("status", "")).lower() != "success":
                        last_detail = f"HTTP {response.status_code}: {_redact_shortener_detail(data.get('message') or raw, api_token, destination)}"
                        raise RuntimeError(last_detail)
                    result = data.get("shortenedUrl") or data.get("shortened_url") or data.get("shortened_url")
                else:
                    result = raw

                result = str(result or "").strip().strip('"').strip("'")
                parsed = urlparse(result)
                if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                    last_detail = f"HTTP {response.status_code}: response did not contain a valid shortened URL ({_redact_shortener_detail(result or raw, api_token, destination)})"
                    raise RuntimeError(last_detail)
                return result
        except Exception as exc:
            last_detail = _redact_shortener_detail(str(exc), api_token, destination)
            if attempt < 3:
                await asyncio.sleep(attempt)
    raise RuntimeError(f"{label} failed after 3 attempts: {last_detail}")


async def shorten_softurl(cfg, destination):
    return await _shorten_http(cfg, cfg.softurl_base_url, cfg.softurl_api, destination, "Softurl")


# Force Subscribe
# ---------------------------

async def missing_channels(bot, user_id, channels):
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in {"left", "kicked"}:
                missing.append(channel)
        except TelegramError as exc:
            log.warning("FSub check failed for %s: %s", channel, exc)
            missing.append(channel)
    return missing


def fsub_keyboard(cfg, gate_id):
    rows = []
    for i, link in enumerate(cfg.fsub_links, 1):
        rows.append(
            [InlineKeyboardButton(f"📢 Join Channel {i}", url=link)]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Verify Subscription", callback_data=f"fsub:{gate_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


# ---------------------------
# Presentation
# ---------------------------

def result_text(movie):
    title = escape(str(movie.get("title") or "Movie"))
    meta = [
        movie.get("quality"),
        movie.get("language"),
        movie.get("season"),
        movie.get("size"),
    ]
    meta = [escape(str(x)) for x in meta if x]
    if meta:
        return f"🎬 <b>{title}</b>\n\n" + " • ".join(meta)
    return f"🎬 <b>{title}</b>"


def result_keyboard(movie, bot_username):
    url = create_deep_linked_url(bot_username, f"file_{movie['_id']}")
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎬 Get Movie", url=url)]]
    )


def filter_keyboard(results, search_id):
    groups = [("Quality", "quality"), ("Language", "language"), ("Season", "season")]
    rows = []
    for label, key in groups:
        values = sorted({str(m.get(key)) for m in results if m.get(key)})
        if values:
            row = []
            for idx, value in enumerate(values[:6]):
                row.append(
                    InlineKeyboardButton(
                        value[:20],
                        callback_data=f"filter:{key}:{search_id}:{idx}",
                    )
                )
            rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------
# Premium presentation
# ---------------------------

WELCOME_HELP = (
    "🔎 <b>HOW TO SEARCH</b>\n\n"
    "Just type the <b>movie or series name</b> in the Request Group.\n\n"
    "✅ Use the correct spelling.\n"
    "✅ Type only the title.\n"
    "❌ Don't add emojis or long descriptions.\n"
    "❌ Don't write full season/episode details in the title.\n\n"
    "The bot will find the available matches automatically."
)
ABOUT_TEXT = (
    "📖 <b>ABOUT CINEMA HUB OG</b>\n\n"
    "Cinema HUB OG was created with one simple goal: to make movie and series discovery faster, cleaner and easier for everyone who needs it.\n\n"
    "This project is built with passion around automation, search and a smooth user experience, with constant improvements to verification and result delivery.\n\n"
    "👑 <b>Developer:</b> {developer_name}\n"
    "💙 <b>Contact:</b> @{developer_username}\n\n"
    "Thank you for supporting the project and helping it grow."
)
UPGRADE_TEXT = (
    "💎 <b>SUPPORT THE PROJECT</b>\n\n"
    "You can use the basic bot without paying anything.\n\n"
    "If you enjoy the experience and want to help me keep building more powerful, creative and premium Telegram automation, you can support the developer.\n\n"
    "Your support helps with development, hosting, maintenance and new features.\n\n"
    "✨ <b>Want to support the project?</b>\n"
    "👉 @thevisionaryoffc\n\n"
    "Thank you for believing in the work. ❤️"
)

def time_greeting(tz_name="Asia/Kolkata"):
    try:
        hour=datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).hour
    except Exception:
        hour=datetime.now().astimezone().hour
    if 5 <= hour < 12: return "GOOD MORNING 🌅"
    if 12 <= hour < 17: return "GOOD AFTERNOON ☀️"
    if 17 <= hour < 21: return "GOOD EVENING 🌆"
    return "GOOD NIGHT 🌙"

def main_menu_keyboard(cfg):
    rows=[]
    if cfg.bot_username:
        rows.append([InlineKeyboardButton("🔰 ADD ME TO YOUR GROUP 🔰", url=f"https://t.me/{cfg.bot_username}?startgroup=true")])
    rows += [
        [InlineKeyboardButton("HELP 📢", callback_data="menu:help"), InlineKeyboardButton("ABOUT 📖", callback_data="menu:about")],
        [InlineKeyboardButton("TOP SEARCHING ⭐", callback_data="menu:top"), InlineKeyboardButton("UPGRADE 💎", callback_data="menu:upgrade")],
    ]
    return InlineKeyboardMarkup(rows)

def request_group_url(cfg=None):
    return "https://t.me/moviesearchoffc"

def request_group_prompt(user):
    name = escape(user.first_name or "Friend")
    return (
        f"🎬 <b>HEY {name} — YOUR SEARCH DESERVES THE HUB.</b>\n\n"
        "All movie & series searching is handled inside the <b>Cinema HUB OG Request Group</b>.\n\n"
        "Type your title there. If it is already indexed, the Hub will find the closest match automatically.\n\n"
        "<i>Tip: type only the title for the fastest match.</i>"
    )

def premium_welcome_text(user, until):
    name = escape(user.first_name or "Premium Member")
    until_text = until.strftime("%d %b %Y") if until else "Active"
    return (
        f"👑 <b>WELCOME BACK, {name.upper()}.</b>\n\n"
        "<b>CINEMA HUB OG • PREMIUM ACCESS</b>\n"
        "Your membership is active. The usual barriers stay behind the door.\n\n"
        f"◈ <b>MEMBERSHIP:</b> ACTIVE\n◈ <b>VALID UNTIL:</b> {until_text}\n\n"
        "🎬 <b>Type any movie or series name right here.</b>\n"
        "The Hub will search privately and return your result instantly."
    )

def premium_only_card():
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "│  👑 <b>PREMIUM ONLY</b>  │\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<b>THIS FEATURE IS FOR PREMIUM USERS ONLY.</b>\n\n"
        "Unlock private search, direct files and the browser <b>Stream & Download</b> experience with Cinema HUB OG Premium."
    )

def premium_status_text(doc):
    if not doc:
        return (
            "<pre>╭────────────────────────╮\n"
            "│       👑 PREMIUM       │\n"
            "│                        │\n"
            "│ STATUS     INACTIVE    │\n"
            "│                        │\n"
            "╰────────────────────────╯</pre>\n\n"
            "Choose a plan to activate your Cinema HUB OG membership."
        )
    until = doc.get("premium_until")
    until = until.replace(tzinfo=timezone.utc) if until and until.tzinfo is None else until
    active = bool(until and until > datetime.now(timezone.utc))
    if not active:
        return (
            "<pre>╭────────────────────────╮\n"
            "│       👑 PREMIUM       │\n"
            "│                        │\n"
            "│ STATUS     EXPIRED     │\n"
            "│                        │\n"
            "╰────────────────────────╯</pre>\n\n"
            "Your Premium membership has expired. Choose a plan to renew it."
        )
    until_text = until.strftime("%d %b %Y")
    plan_id = str(doc.get("premium_plan") or "").strip()
    plan = PREMIUM_PLANS.get(plan_id, {})
    plan_no = escape(str(plan.get("label") or "PREMIUM"))
    price = doc.get("premium_last_price")
    price_text = f"₹{price}" if price is not None else "—"
    period = str(plan.get("period") or "Membership")
    plan_line = f"{price_text} / {period.upper()}"
    return (
        "<pre>╭────────────────────────╮\n"
        "│       👑 PREMIUM       │\n"
        "│                        │\n"
        f"│ {plan_no:<23} │\n"
        f"│ {plan_line:<23} │\n"
        "│                        │\n"
        "│ STATUS     ACTIVE      │\n"
        "│                        │\n"
        "│ VALID UNTIL             │\n"
        f"│ {until_text:<23} │\n"
        "╰────────────────────────╯</pre>"
    )

def premium_status_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 START PREMIUM SEARCH", callback_data="premium:start")],
        [InlineKeyboardButton("💎 VIEW PLANS", callback_data="premium:plans")],
        [InlineKeyboardButton("← BACK", callback_data="menu:main")],
    ])


def premium_fsub_text():
    return (
        "🔒 <b>CINEMA HUB OG — SUBSCRIPTION CHECK</b>\n\n"
        "Force Subscribe is required for <b>every member</b>, including Premium members.\n\n"
        "<b>1.</b> Join every required channel\n"
        "<b>2.</b> Tap <b>VERIFY SUBSCRIPTION</b>\n"
        "<b>3.</b> Continue where you left off"
    )

def premium_fsub_keyboard(cfg, gate_id):
    rows=[[InlineKeyboardButton(f"📢 JOIN UPDATE CHANNEL {i}",url=link)] for i,link in enumerate(cfg.fsub_links,1)]
    rows.append([InlineKeyboardButton("🔄 TRY AGAIN 🔄",callback_data=f"fsub:{gate_id}")])
    return InlineKeyboardMarkup(rows)

def search_button(movie,cfg,number):
    # Kept for compatibility with the existing codebase. Search results are
    # now rendered as full clickable text links instead of inline buttons.
    title=str(movie.get("title") or "Untitled")
    quality=str(movie.get("quality") or "Quality ?")
    language=str(movie.get("language") or "Language ?")
    size=str(movie.get("size") or "Size ?")
    label=re.sub(r"\s+"," ",f"{number}. {size} • {quality} • {language} • {title}").strip()
    if len(label)>58: label=label[:55].rstrip()+"…"
    return InlineKeyboardButton(label,url=create_deep_linked_url(cfg.bot_username,f"file_{movie['_id']}"))

def compact_caption_preview(movie, max_chars=180):
    """Preserve the important portion of the operator-written caption.

    Search results stay compact enough for Telegram's message-size limits while
    still showing the original caption's useful episode/audio/quality/source text.
    """
    caption = _strip_our_database_footer(str(movie.get("caption") or "")).strip()
    caption = clean_database_caption(caption) if caption else ""
    if not caption:
        return ""
    title_key = normalize(str(movie.get("title") or ""))
    lines = []
    for raw in caption.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if title_key and normalize(line) == title_key:
            continue
        lines.append(line)
        if len(" • ".join(lines)) >= max_chars:
            break
    preview = " • ".join(lines).strip(" •")
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1].rstrip() + "…"
    return preview


def search_button(movie,cfg,number):
    # Kept for compatibility with the existing codebase. Search results are
    # rendered as clickable text lines below.
    title=str(movie.get("title") or "Untitled")
    quality=str(movie.get("quality") or "Quality ?")
    language=str(movie.get("language") or "Language ?")
    size=str(movie.get("size") or "Size ?")
    label=re.sub(r"\s+"," ",f"{number}. {size} • {quality} • {language} • {title}").strip()
    if len(label)>58: label=label[:55].rstrip()+"…"
    return InlineKeyboardButton(label,url=create_deep_linked_url(cfg.bot_username,f"file_{movie['_id']}"))


def search_result_link(movie,cfg,number):
    """Return one compact clickable result plus a caption-preserving preview."""
    size = str(movie.get("size") or "").strip()
    if not size:
        size = format_file_size(movie.get("file_size")) or ""

    filename = clean_result_name(movie.get("filename") or movie.get("title") or "Untitled")
    if len(filename) > 145:
        filename = filename[:142].rstrip() + "…"
    label = f"{number}. {size} | {filename}" if size else f"{number}. {filename}"
    url = create_deep_linked_url(cfg.bot_username, f"file_{movie['_id']}")
    line = f'<a href="{escape(url, quote=True)}"><b>{escape(label)}</b></a>'
    preview = compact_caption_preview(movie)
    if preview:
        line += f'\n<i>{escape(preview)}</i>'
    return line


def search_header(query,total_results,page,pages,filters_,page_size=8):
    active=[f"{k.title()}: {escape(str(v))}" for k,v in filters_.items() if k in {"quality","language","season"} and v]
    shown_start=page*page_size+1 if total_results else 0
    shown_end=min((page+1)*page_size,total_results)
    return (
        "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
        "│ <i>THE CINEMATIC FILE INDEX</i>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🔎 <b>{escape(query)}</b>\n"
        f"◈ <b>{total_results}</b> matches • {shown_start}-{shown_end} shown"
        + ("\n🎛 <b>"+" • ".join(active)+"</b>" if active else "")
        + "\n\n<blockquote>Tap a file to continue.</blockquote>"
    )


def search_result_text(cfg,results,page,page_size):
    lines=[]
    start_number=page*page_size+1
    for number,movie in enumerate(results,start=start_number):
        lines.append(search_result_link(movie,cfg,number))
    return "\n\n".join(lines)


def search_markup(cfg,results,search_id,page,pages,batch_url,total_results=None):
    rows=[]
    total_results = int(total_results or 0)
    batch_count = min(total_results, cfg.send_all_limit) if total_results else cfg.send_all_limit
    if total_results and total_results <= cfg.send_all_limit:
        batch_label = f"📦 SEND ALL {total_results} FILES"
    elif total_results:
        batch_label = f"📦 SEND {batch_count} / {total_results} FILES"
    else:
        batch_label = "📦 SEND ALL FILES"
    rows.append([InlineKeyboardButton(batch_label, url=batch_url)])
    rows.append([
        InlineKeyboardButton("🎞 QUALITY", callback_data=f"filter_menu:quality:{search_id}"),
        InlineKeyboardButton("🌐 LANGUAGE", callback_data=f"filter_menu:language:{search_id}"),
        InlineKeyboardButton("📺 SEASON", callback_data=f"filter_menu:season:{search_id}"),
    ])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("‹ PREV",callback_data=f"page:{page-1}:{search_id}"))
    nav.append(InlineKeyboardButton(f"{page+1} / {pages}",callback_data="noop"))
    if page<pages-1: nav.append(InlineKeyboardButton("NEXT ›",callback_data=f"page:{page+1}:{search_id}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def create_batch_for_search(context,user_id,query,filters_,limit):
    db=context.application.bot_data["db"]
    cfg=context.application.bot_data["cfg"]
    all_results = await db.find_movies(query, min(cfg.send_all_limit, limit), filters_ or {}, skip=0)
    batch_id=new_token(8)
    await db.create_batch(
        batch_id, user_id, [str(m["_id"]) for m in all_results],
        datetime.now(timezone.utc)+timedelta(minutes=30)
    )
    return batch_id


async def _persist_search_result_message(message, db, search_id, user_id, cfg):
    """Persist the first result message and keep the original request on the same timer."""
    await db.set_search_result_message_once(
        search_id,
        user_id,
        message.chat_id,
        message.message_id,
        datetime.now(timezone.utc) + timedelta(seconds=cfg.search_result_delete_after),
    )


async def render_search_message(
    message,
    context,
    results,
    search_id,
    page,
    query,
    user_id,
    total_results=None,
):
    cfg=context.application.bot_data["cfg"]
    db=context.application.bot_data["db"]
    doc=await db.get_search(search_id,user_id)
    filters_=(doc or {}).get("filters") or {}
    size=cfg.page_size
    if total_results is None:
        total_results=await db.count_movies(query,filters_)
    pages=max(1,(total_results+size-1)//size)
    page=max(0,min(page,pages-1))

    # SEND ALL FILES is backed by the database query, not only the visible page.
    # This preserves the existing button while making it actually mean "all results"
    # (bounded by SEND_ALL_LIMIT).
    batch_id=await create_batch_for_search(context,user_id,query,filters_,cfg.send_all_limit)
    markup=search_markup(cfg,results,search_id,page,pages,create_deep_linked_url(cfg.bot_username,f"all_{batch_id}"),total_results)
    text=search_header(query,total_results,page,pages,filters_,cfg.page_size)
    text += "\n\n" + search_result_text(cfg,results,page,cfg.page_size)

    # Fast, single-edit reveal. Avoid artificial sleeps that make the bot feel slow.
    try:
        await message.edit_text(text,reply_markup=markup,parse_mode="HTML")
        await _persist_search_result_message(message, db, search_id, user_id, cfg)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            sent = await message.reply_text(text,reply_markup=markup,parse_mode="HTML")
            await _persist_search_result_message(sent, db, search_id, user_id, cfg)


async def cleanup_expired_search_messages(context):
    """Delete both the user's original search and bot result at one shared expiry."""
    db = context.application.bot_data.get("db")
    if db is None:
        return
    try:
        docs = await db.expired_search_messages(limit=100)
        for doc in docs:
            sid = doc.get("search_id")
            if not sid:
                continue

            request_chat_id = doc.get("request_chat_id")
            request_message_id = doc.get("request_message_id")
            if request_chat_id and request_message_id and not doc.get("request_deleted_at"):
                request_deleted = False
                try:
                    await context.bot.delete_message(
                        chat_id=int(request_chat_id),
                        message_id=int(request_message_id),
                    )
                    request_deleted = True
                except BadRequest as exc:
                    text = str(exc).lower()
                    # Telegram reports already-missing messages as BadRequest.
                    if "not found" in text or "message to delete" in text:
                        request_deleted = True
                    else:
                        log.debug("Could not delete user search %s: %s", sid, exc)
                except (Forbidden, TelegramError) as exc:
                    log.debug("Could not delete user search %s: %s", sid, exc)
                except Exception as exc:
                    log.debug("Could not delete user search %s: %s", sid, exc)
                if request_deleted:
                    try:
                        await db.mark_search_request_deleted(sid)
                    except Exception:
                        log.exception("Could not mark user search %s as deleted", sid)

            result_chat_id = doc.get("result_chat_id")
            result_message_id = doc.get("result_message_id")
            if result_chat_id and result_message_id and not doc.get("result_deleted_at"):
                result_deleted = False
                try:
                    await context.bot.delete_message(
                        chat_id=int(result_chat_id),
                        message_id=int(result_message_id),
                    )
                    result_deleted = True
                except BadRequest as exc:
                    text = str(exc).lower()
                    if "not found" in text or "message to delete" in text:
                        result_deleted = True
                    else:
                        log.debug("Could not delete search result %s: %s", sid, exc)
                except (Forbidden, TelegramError) as exc:
                    log.debug("Could not delete search result %s: %s", sid, exc)
                except Exception as exc:
                    log.debug("Could not delete search result %s: %s", sid, exc)
                if result_deleted:
                    try:
                        await db.mark_search_result_deleted(sid)
                    except Exception:
                        log.exception("Could not mark search result %s as deleted", sid)
    except Exception:
        log.exception("Expired search-message cleanup failed")

def menu_text(kind, cfg=None):
    if kind=="help": return WELCOME_HELP
    if kind=="about":
        cfg = cfg or type("Cfg", (), {"developer_name": "Cinema HUB OG Developer", "developer_username": "thevisionaryoffc"})()
        return ABOUT_TEXT.format(developer_name=escape(cfg.developer_name), developer_username=escape(cfg.developer_username))
    if kind=="upgrade": return UPGRADE_TEXT
    return None

async def _edit_menu_message(query, text, markup):
    message = query.message
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        else:
            await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def ensure_user_fsub_for_action(user_id, context, route_type="premium_start", query_text=""):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    if not (cfg.require_fsub and cfg.fsub_channels):
        return True
    missing = await missing_channels(context.bot, user_id, cfg.fsub_channels)
    if not missing:
        return True
    gate_id = new_token(10)
    await db.create_fsub_gate(
        gate_id, user_id, [], datetime.now(timezone.utc)+timedelta(minutes=30),
        context={"type": route_type, "query": query_text},
    )
    return gate_id


async def payment_admin_callback(update, context):
    q = update.callback_query
    cfg = context.application.bot_data["cfg"]
    if not q or q.from_user.id not in cfg.admin_ids:
        if q:
            await q.answer("Not authorized.", show_alert=True)
        return
    parts = str(q.data or "").split(":", 2)
    if len(parts) != 3:
        await q.answer("Invalid payment action.", show_alert=True)
        return
    action, request_id = parts[1], parts[2]
    manager = context.application.bot_data.get("premium")
    if manager is None:
        await q.answer("Premium manager is unavailable.", show_alert=True)
        return
    if action == "approve":
        result = await manager.approve_request(request_id, q.from_user.id)
    elif action == "reject":
        result = await manager.reject_request(request_id, q.from_user.id)
    else:
        await q.answer("Unknown action.", show_alert=True)
        return
    await q.answer(result.get("toast", "Done"), show_alert=result.get("alert", False))
    try:
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        pass


async def premium_callback(update, context):
    q = update.callback_query
    if not q:
        return
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    parts = str(q.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action in {"buy", "plans"}:
        await q.answer()
        loader, started = await show_cinematic_loader(
            q.message, "👑 <b>OPENING CINEMA HUB OG PREMIUM</b>\nPreparing your membership options…"
        )
        await finish_cinematic_loader(loader, started)
        await _edit_menu_message(q, premium_plans_text(), premium_plans_keyboard())
        return

    if action == "status":
        await q.answer()
        loader, started = await show_cinematic_loader(
            q.message, "✨ <b>CHECKING YOUR MEMBERSHIP</b>\nRefreshing your Premium status…"
        )
        doc = await db.get_user(q.from_user.id)
        await finish_cinematic_loader(loader, started)
        await _edit_menu_message(q, premium_status_text(doc), premium_status_keyboard())
        return

    if action == "start":
        if not await db.is_premium(q.from_user.id):
            await q.answer("Premium is not active.", show_alert=True)
            await q.message.reply_text(
                premium_only_card(),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 VIEW PLANS", callback_data="premium:plans")]]),
                parse_mode="HTML",
            )
            return
        gate_id = await ensure_user_fsub_for_action(q.from_user.id, context, "premium_start")
        if gate_id is not True:
            await q.answer("Join the required channels first.", show_alert=True)
            await q.message.reply_text(
                premium_fsub_text(),
                reply_markup=premium_fsub_keyboard(cfg, gate_id),
                parse_mode="HTML",
            )
            return
        await q.answer("Premium search ready ✅")
        await q.message.reply_text("🎬 <b>Type your movie or series name right here.</b>\n\nI’ll search privately and return the fastest available match.", parse_mode="HTML")
        return

    if action == "plan" and len(parts) >= 3:
        plan_id = parts[2]
        if plan_id not in PREMIUM_PLANS:
            await q.answer("Invalid plan.", show_alert=True)
            return
        await q.answer()
        loader, started = await show_cinematic_loader(
            q.message, "💳 <b>PREPARING SECURE PAYMENT</b>\nSetting up your UPI activation request…"
        )
        request_id = await db.create_premium_payment_request(q.from_user.id, plan_id)
        owner_url = (
            f"https://t.me/{cfg.payment_bot_username}?start=pay_{request_id}"
            if cfg.payment_bot_username
            else "https://t.me/"
        )
        await finish_cinematic_loader(loader, started)
        await q.message.reply_text(
            payment_text(plan_id),
            reply_markup=payment_keyboard(owner_url, cfg.premium_qr_url),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if action == "skip" and len(parts) >= 3:
        token = parts[2]
        action_doc = await db.get_premium_action(token, q.from_user.id)
        if not action_doc:
            await q.answer("This option has expired. Open the file again.", show_alert=True)
            return
        if not await db.is_premium(q.from_user.id):
            await db.consume_premium_action(token, q.from_user.id)
            await q.answer("Premium membership required.", show_alert=True)
            await q.message.reply_text(
                premium_only_card(),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 VIEW PLANS", callback_data="premium:plans")]]),
                parse_mode="HTML",
            )
            return

        action_doc = await db.consume_premium_action(token, q.from_user.id)
        if not action_doc:
            await q.answer("This option has already been used.", show_alert=True)
            return

        await q.answer("Premium confirmed ✅")
        await deliver(q, context, action_doc["movie_ids"], direct=True)
        return

    await q.answer()


async def premium_admin_command(update, context, kind):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    args = (update.message.text or "").split()
    if kind == "add":
        if len(args) < 3:
            await update.message.reply_text("Usage: /premium_add <user_id> <days>")
            return
        try:
            uid, days = int(args[1]), int(args[2])
        except ValueError:
            await update.message.reply_text("User ID and days must be numbers.")
            return
        until = await db.activate_premium(uid, days, "manual", 0, source="admin")
        try:
            await context.bot.set_chat_member_tag(cfg.request_group, uid, tag=cfg.premium_member_tag)
        except Exception as exc:
            log.info("Could not set manual premium group tag for %s: %s", uid, exc)
        await update.message.reply_text(
            f"✅ Premium added for <code>{uid}</code> until <b>{until.strftime('%d %b %Y %H:%M UTC')}</b>.",
            parse_mode="HTML",
        )
        return
    if kind == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /premium_remove <user_id>")
            return
        try:
            uid = int(args[1])
        except ValueError:
            await update.message.reply_text("User ID must be a number.")
            return
        await db.remove_premium(uid)
        try:
            await context.bot.set_chat_member_tag(cfg.request_group, uid, tag=None)
        except Exception as exc:
            log.info("Could not clear manual premium group tag for %s: %s", uid, exc)
        await update.message.reply_text(f"✅ Premium removed for <code>{uid}</code>.", parse_mode="HTML")
        return
    if kind == "status":
        if len(args) < 2:
            await update.message.reply_text("Usage: /premium_status <user_id>")
            return
        try:
            uid = int(args[1])
        except ValueError:
            await update.message.reply_text("User ID must be a number.")
            return
        doc = await db.get_user(uid)
        until = (doc or {}).get("premium_until")
        active = bool(until and until > datetime.now(timezone.utc))
        await update.message.reply_text(
            f"💎 <b>PREMIUM STATUS</b>\n\n"
            f"User: <code>{uid}</code>\n"
            f"Active: <b>{active}</b>\n"
            f"Until: <b>{escape(str(until) if until else 'Not active')}</b>",
            parse_mode="HTML",
        )
        return
    if kind == "list":
        rows = await db.premium_list(50)
        if not rows:
            await update.message.reply_text("No active premium users found.")
            return
        lines = ["💎 <b>ACTIVE PREMIUM USERS</b>", ""]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. <code>{row.get('user_id')}</code> → {escape(str(row.get('premium_until')))}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def referral_screen_text(user_id, context):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    bot_username = cfg.bot_username or (getattr(context.bot, "username", "") or "")
    if not bot_username:
        me = await context.bot.get_me()
        bot_username = getattr(me, "username", "") or ""
        cfg.bot_username = bot_username
    if not bot_username:
        raise RuntimeError("BOT_USERNAME is unavailable for referral links")
    link = create_deep_linked_url(bot_username, f"ref_{int(user_id)}")
    summary = await db.referral_summary(user_id)
    return (
        "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
        "│ 🎁 <b>REFER & GET PREMIUM</b> │\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Invite friends to Cinema HUB OG using your personal link.\n\n"
        f"🔗 <b>Your invite link</b>\n<code>{escape(link)}</code>\n\n"
        f"✅ <b>Successful referrals:</b> {summary['qualified']}\n"
        f"⏳ <b>Pending first delivery:</b> {summary['pending']}\n\n"
        "A referral is counted after a new user joins through your link, completes the required subscription/verification steps and receives their first file.\n\n"
        "<i>Premium rewards are credited automatically after qualification.</i>"
    ), link


async def show_referral_screen(query, context):
    text, link = await referral_screen_text(query.from_user.id, context)
    share_url = "https://t.me/share/url?" + f"url={quote(link, safe='')}&text={quote('Join Cinema HUB OG for movie and series search.', safe='')}"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 SHARE MY INVITE LINK", url=share_url)],
        [InlineKeyboardButton("🔄 REFRESH REFERRAL STATUS", callback_data="menu:referral")],
        [InlineKeyboardButton("💎 BACK TO PREMIUM", callback_data="menu:upgrade")],
        [InlineKeyboardButton("‹ BACK TO MAIN MENU", callback_data="menu:main")],
    ])
    await _edit_menu_message(query, text, markup)


async def menu_callback(update,context):
    q=update.callback_query
    await q.answer()
    action=q.data.split(":",1)[1]
    if action=="main":
        cfg=context.application.bot_data["cfg"]
        bot_username=cfg.bot_username or (getattr(context.bot,"username","") or "")
        link=f"https://t.me/{bot_username}" if bot_username else "#"
        text=("🚩 <b>JAI SHRI RAM 🚩</b>\n\n"
              f"👋 <b>HEY {escape(q.from_user.first_name or 'Friend')}</b>, {time_greeting(cfg.greeting_timezone)}\n\n"
              f"🤖 I AM <a href=\"{link}\">Cinema HUB OG</a>,\n"
              "<b>THE MOST POWERFUL AUTO FILTER BOT WITH PREMIUM FEATURES.</b>\n\n"
              "Here you get a clean, fast and premium movie-search experience with smart filters, smooth verification and direct result access.")
        await _edit_menu_message(q,text,main_menu_keyboard(cfg)); return
    if action=="top":
        rows=await context.application.bot_data["db"].top_searches(10)
        text="⭐ <b>TOP SEARCHING</b>\n\n"+("\n".join(f"<b>{i}.</b> {escape(str(r.get('query') or 'Unknown'))} — <code>{r.get('count',0)}</code> searches" for i,r in enumerate(rows,1)) if rows else "No searches have been recorded yet.")
        await _edit_menu_message(q,text,InlineKeyboardMarkup([[InlineKeyboardButton("‹ BACK TO MAIN MENU",callback_data="menu:main")]]))
        return
    if action == "upgrade":
        loader, started = await show_cinematic_loader(
            q.message, "👑 <b>ENTERING THE PREMIUM EXPERIENCE</b>\nPreparing your membership screen…"
        )
        await finish_cinematic_loader(loader, started)
        await _edit_menu_message(q, premium_plans_text(), premium_plans_keyboard())
        return
    if action in {"referral", "referral_refresh"}:
        loader, started = await show_cinematic_loader(
            q.message, "🎁 <b>PREPARING YOUR INVITE LINK</b>\nChecking your referral progress…"
        )
        await finish_cinematic_loader(loader, started)
        try:
            await show_referral_screen(q, context)
        except Exception as exc:
            log.exception("Could not create referral screen for user %s", q.from_user.id)
            await q.message.reply_text(
                "⚠️ <b>Referral link is temporarily unavailable.</b>\nPlease set BOT_USERNAME in Render and try again.",
                parse_mode="HTML",
            )
        return
    text=menu_text(action, context.application.bot_data["cfg"]) or "Unavailable."
    await _edit_menu_message(q,text,InlineKeyboardMarkup([[InlineKeyboardButton("‹ BACK TO MAIN MENU",callback_data="menu:main")]]))


# ---------------------------

async def deliver(update, context, movie_ids, direct=False):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id
    is_premium_user = await db.is_premium(user_id)
    sent_ids = []

    for movie_id in movie_ids:
        try:
            from bson import ObjectId
            doc = await db.movies.find_one({"_id": ObjectId(movie_id)})
        except Exception:
            doc = None
        if not doc:
            continue
        try:
            markup = None
            if is_premium_user and cfg.stream_base_url and cfg.stream_signing_secret:
                stream_url = f"{cfg.stream_base_url}/watch/{create_stream_token(cfg, user_id, doc['_id'])}"
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 STREAM & DOWNLOAD", url=stream_url)]])

            sent = await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=cfg.database_channel,
                message_id=doc["message_id"],
                reply_markup=markup,
            )
            sent_ids.append(sent.message_id)
        except Exception:
            log.exception("Failed to copy database message %s", doc.get("message_id"))

    if not sent_ids:
        await update.effective_message.reply_text("⚠️ <b>The requested file is unavailable right now.</b>", parse_mode="HTML")
        return

    # Referral qualification is tied to successful first file delivery, not just /start.
    try:
        reward = await db.qualify_referral_for_user(user_id)
        if reward:
            referrer_id = int(reward["referrer_id"])
            until = reward.get("premium_until")
            until_text = until.strftime("%d %b %Y, %H:%M UTC") if until else "your updated expiry"
            await context.bot.send_message(
                chat_id=referrer_id,
                text=(
                    "🎉 <b>REFERRAL REWARD UNLOCKED!</b>\n\n"
                    "Your invited friend completed their first successful delivery.\n"
                    "✅ <b>Reward:</b> 1 day of Cinema HUB OG Premium\n"
                    f"👑 <b>Premium valid until:</b> {escape(until_text)}"
                ),
                parse_mode="HTML",
            )
            try:
                await context.bot.set_chat_member_tag(cfg.request_group, referrer_id, tag=cfg.premium_member_tag)
            except Exception as tag_exc:
                log.info("Could not set referral Premium tag for %s: %s", referrer_id, tag_exc)
    except Exception:
        log.exception("Referral reward processing failed for first delivery by user %s", user_id)

    notice_text = (
        "⚠️ <b>TEMPORARY DELIVERY</b>\n\n"
        "This file will be deleted from the bot chat in <b>5 minutes</b>.\n"
        "Save or forward it somewhere safe if you need it later."
    )
    notice = await update.effective_message.reply_text(notice_text, parse_mode="HTML")
    sent_ids.append(notice.message_id)
    context.job_queue.run_once(delete_delivered, when=cfg.delete_after, data={"chat_id": user_id, "message_ids": sent_ids})


async def delete_delivered(context):
    data = context.job.data
    for mid in data["message_ids"]:
        try:
            await context.bot.delete_message(data["chat_id"], mid)
        except Exception:
            pass


# ---------------------------
# Start / verification
# ---------------------------

async def send_main_menu(update, context):
    cfg = context.application.bot_data["cfg"]
    user = update.effective_user
    bot_username = cfg.bot_username or (getattr(context.bot, "username", "") or "")
    bot_link = f"https://t.me/{bot_username}" if bot_username else "https://t.me/moviesearchoffc"
    text = (
        "🚩 <b>JAI SHRI RAM 🚩</b>\n\n"
        f"👋 <b>HEY {escape(user.first_name or 'Friend')}</b>, {time_greeting(cfg.greeting_timezone)}\n\n"
        f'🤖 I AM <a href="{bot_link}">Cinema HUB OG</a>,\n'
        "<b>THE MOST POWERFUL AUTO FILTER BOT WITH PREMIUM FEATURES.</b>\n\n"
        "Your cinema search starts here — clean results, smart filters and a smoother verification experience."
    )
    local_logo = os.path.join(os.path.dirname(__file__), "stream_server", "assets", "cinema_hub_og_logo.jpg")
    if os.path.isfile(local_logo):
        try:
            return await update.effective_message.reply_photo(
                photo=InputFile(local_logo), caption=text,
                reply_markup=main_menu_keyboard(cfg), parse_mode="HTML",
            )
        except Exception:
            log.exception("Official Cinema HUB OG logo could not be sent; showing the welcome text without a substitute image.")
    return await update.effective_message.reply_text(
        text, reply_markup=main_menu_keyboard(cfg), parse_mode="HTML"
    )

async def send_premium_menu(update, context):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    doc = await db.get_user(update.effective_user.id)
    welcome = premium_welcome_text(update.effective_user, doc.get("premium_until") if doc else None)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 PREMIUM STATUS", callback_data="premium:status")],
        [InlineKeyboardButton("💎 VIEW PLANS", callback_data="premium:plans")],
        [InlineKeyboardButton("🎁 REFER & GET PREMIUM", callback_data="menu:referral")],
        [InlineKeyboardButton("🎬 START PREMIUM SEARCH", callback_data="premium:start")],
    ])
    local_logo = os.path.join(os.path.dirname(__file__), "stream_server", "assets", "cinema_hub_og_logo.jpg")
    if os.path.isfile(local_logo):
        try:
            return await update.effective_message.reply_photo(
                photo=InputFile(local_logo), caption=welcome,
                reply_markup=markup, parse_mode="HTML",
            )
        except Exception:
            log.exception("Official logo failed on Premium welcome; using text only.")
    await update.effective_message.reply_text(welcome, reply_markup=markup, parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message:
        return
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    is_new_user = await db.record_user(
        update.effective_user.id,
        username=update.effective_user.username or "",
        first_name=update.effective_user.first_name or "",
    )
    payload = context.args[0] if context.args else ""
    if payload.startswith("ref_"):
        try:
            await db.register_referral(int(payload[4:]), update.effective_user.id, is_new_user)
        except (TypeError, ValueError):
            log.info("Ignored malformed referral payload")
        # Continue normal /start flow; referral rewards only after successful delivery.

    # Deep links must preserve the content-selection flow. F-Sub is applied before
    # premium/free entitlement checks so Premium can never bypass it.
    if payload.startswith("file_"):
        await begin_gate(update, context, [payload[5:]])
        return
    if payload.startswith("all_"):
        batch = await db.consume_batch(payload[4:], update.effective_user.id)
        if not batch:
            await update.effective_message.reply_text("❌ <b>This batch link is expired or invalid.</b>", parse_mode="HTML")
            return
        await begin_gate(update, context, batch["movie_ids"])
        return
    if payload.startswith("vs_"):
        await verification_return(update, context, payload[3:])
        return
    if payload.startswith("sv_"):
        # Backward compatibility for older one-step verification links.
        await verification_return(update, context, payload[3:])
        return

    premium = await db.is_premium(update.effective_user.id)
    await (send_premium_menu(update, context) if premium else send_main_menu(update, context))

    if cfg.require_fsub and cfg.fsub_channels:
        missing = await missing_channels(context.bot, update.effective_user.id, cfg.fsub_channels)
        if missing:
            gate_id = new_token(10)
            await db.create_fsub_gate(
                gate_id, update.effective_user.id, [],
                datetime.now(timezone.utc)+timedelta(minutes=30),
                context={"type": "premium_start" if premium else "free_start"},
            )
            await update.effective_message.reply_text(
                premium_fsub_text(), reply_markup=premium_fsub_keyboard(cfg, gate_id), parse_mode="HTML"
            )
            return

    if premium:
        return
    # The welcome/logo is sent first; exactly one Request Here prompt arrives after
    # the configured 2–3 second pause, and only after F-Sub has passed.
    await send_request_prompt_after_delay(context, update.effective_chat.id, update.effective_user)

async def begin_gate(update, context, movie_ids):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id
    clean_ids = [str(x) for x in movie_ids]

    if cfg.require_fsub and cfg.fsub_channels:
        missing = await missing_channels(context.bot, user_id, cfg.fsub_channels)
        if missing:
            gate_id = new_token(10)
            await db.create_fsub_gate(
                gate_id, user_id, clean_ids, datetime.now(timezone.utc)+timedelta(minutes=30)
            )
            await update.effective_message.reply_text(
                premium_fsub_text(), reply_markup=premium_fsub_keyboard(cfg, gate_id), parse_mode="HTML"
            )
            return

    if await db.is_premium(user_id):
        await deliver(update, context, clean_ids, direct=True)
    elif await db.has_verification_access(user_id):
        await deliver(update, context, clean_ids, direct=True)
    else:
        await send_softurl(update, context, clean_ids)


async def fsub_check(update, context):
    query = update.callback_query
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    gate_id = query.data.split(":", 1)[1]
    missing = await missing_channels(context.bot, query.from_user.id, cfg.fsub_channels)
    if missing:
        await query.answer("Join every required channel first.", show_alert=True)
        return
    gate = await db.consume_fsub_gate(gate_id, query.from_user.id)
    if not gate:
        await query.answer("This verification session has expired. Start again.", show_alert=True)
        return
    await query.answer("Subscription verified ✅")
    movie_ids = gate.get("movie_ids") or []
    if movie_ids:
        if await db.is_premium(query.from_user.id):
            await deliver(query, context, movie_ids, direct=True)
        elif await db.has_verification_access(query.from_user.id):
            await deliver(query, context, movie_ids, direct=True)
        else:
            await send_softurl(query, context, movie_ids)
        return

    route = (gate.get("context") or {}).get("type")
    if route == "premium_search":
        fake = type("Obj", (), {"effective_message": query.message, "effective_user": query.from_user})()
        await perform_premium_search(fake, context, (gate.get("context") or {}).get("query", ""))
        return
    if route == "premium_start":
        await query.message.reply_text(
            "🎬 <b>PREMIUM SEARCH IS READY.</b>\n\nType your movie or series name right here.",
            parse_mode="HTML",
        )
        return
    await send_request_prompt_after_delay(context, query.message.chat_id, query.from_user)


async def _verification_status_text(session):
    if bool(session.get("softurl_verified")):
        return "✅ Softurl verification is complete. Preparing your file..."
    return "🔐 Verification progress: <b>0/1</b> completed."


async def send_softurl(update, context, movie_ids):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if not cfg.require_shortlink:
        await deliver(update, context, movie_ids)
        return
    if await db.has_verification_access(user_id):
        await deliver(update, context, movie_ids, direct=True)
        return
    if not cfg.softurl_api:
        log.error("Softurl verification cannot start; missing environment variable: SOFTURL_API")
        await update.effective_message.reply_text(
            "⚠️ <b>Verification is temporarily unavailable.</b>\n\nThe administrator is checking the shortener connection. Please try again shortly.",
            parse_mode="HTML",
        )
        return

    loader, loader_started = await show_cinematic_loader(
        update.effective_message,
        "🔐 <b>SECURING YOUR FREE ACCESS</b>\nPreparing your verification link…",
    )
    try:
        bot_username = cfg.bot_username or (getattr(context.bot, "username", "") or "")
        if not bot_username:
            try:
                me = await context.bot.get_me()
                bot_username = getattr(me, "username", "") or ""
            except Exception as exc:
                raise RuntimeError("Cannot determine the bot username; set BOT_USERNAME in Render.") from exc
        bot_username = str(bot_username).lstrip("@")
        if not bot_username:
            raise RuntimeError("BOT_USERNAME is empty; set the main bot's public username in Render.")
        cfg.bot_username = bot_username

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=cfg.verification_session_ttl)
        session_id = await db.create_verification_session(user_id, movie_ids, expires)
        soft_destination = create_deep_linked_url(bot_username, f"vs_{session_id}_soft")
        soft = await shorten_softurl(cfg, soft_destination)
    except Exception as exc:
        await finish_cinematic_loader(loader, loader_started)
        safe_error = _redact_shortener_detail(str(exc), cfg.softurl_api)
        log.error("Verification link setup failed: %s", safe_error)
        await update.effective_message.reply_text(
            "⚠️ <b>Verification link could not be generated.</b>\n\nPlease try again in a moment. If this continues, the administrator must check the Softurl API response and the configured BOT_USERNAME.",
            parse_mode="HTML",
        )
        return

    await finish_cinematic_loader(loader, loader_started)
    await update.effective_message.reply_text(
        "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
        "│ 🔐 <b>VERIFICATION REQUIRED</b> │\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Complete the short verification once. After that, you can get free results for <b>6 hours</b> without repeating it.\n\n"
        "<i>Premium members skip this verification and keep their Premium benefits.</i>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 VERIFY SOFTURL", url=soft)],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def verification_return(update, context, payload):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id
    raw_payload = str(payload)
    parts = raw_payload.rsplit("_", 1)
    if len(parts) == 2 and parts[1] == "soft":
        session_id, stage = parts[0], "soft"
    elif raw_payload.isalnum():
        # Backward compatibility for earlier single-step sv_<session_id> links.
        session_id, stage = raw_payload, "soft"
    else:
        await update.effective_message.reply_text("❌ <b>Invalid verification link.</b>", parse_mode="HTML")
        return

    if cfg.require_fsub and cfg.fsub_channels:
        missing = await missing_channels(context.bot, user_id, cfg.fsub_channels)
        if missing:
            await update.effective_message.reply_text(
                "❌ <b>Please join the required channels first.</b>", parse_mode="HTML"
            )
            return

    session = await db.mark_verification_stage(session_id, user_id, stage)
    if not session:
        await update.effective_message.reply_text(
            "❌ <b>This verification session is invalid or expired.</b>\n\nOpen the file again to start a fresh verification.",
            parse_mode="HTML",
        )
        return

    if bool(session.get("softurl_verified")):
        loader, loader_started = await show_cinematic_loader(
            update.effective_message,
            "✨ <b>VERIFICATION COMPLETE</b>\nUnlocking your free access…",
        )
        until = datetime.now(timezone.utc) + timedelta(seconds=cfg.verification_access_ttl)
        await db.set_verification_access(user_id, until)
        await finish_cinematic_loader(loader, loader_started)
        await update.effective_message.reply_text(
            "✅ <b>VERIFICATION COMPLETE</b>\n\n"
            "Your free verification session is active for <b>6 hours</b>.\n"
            "You can now open results without repeating verification.\n\n"
            "<i>Stream & Download remains a Premium-only feature.</i>",
            parse_mode="HTML",
        )
        await deliver(update, context, session["movie_ids"], direct=True)
        return

    await update.effective_message.reply_text(
        f"✅ <b>Softurl verified.</b>\n\n{await _verification_status_text(session)}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# A branded, short-lived animated message used for search, verification and Premium transitions.
CINEMATIC_LOADER_MIN_SECONDS = 3.25


def _cinematic_loader_path():
    return os.path.join(os.path.dirname(__file__), "stream_server", "assets", "cinema_hub_og_loader.gif")


async def show_cinematic_loader(message, caption):
    """Post the custom 3.3-second branded animation; callers delete it before final content."""
    started = time.monotonic()
    path = _cinematic_loader_path()
    try:
        if os.path.isfile(path):
            loader = await message.reply_animation(
                animation=InputFile(path), caption=caption, parse_mode="HTML"
            )
        else:
            loader = await message.reply_text(
                "🎬\n🔎\n✨\n\n<b>CINEMA HUB OG</b>\nPreparing your experience…",
                parse_mode="HTML",
            )
        return loader, started
    except Exception:
        log.exception("Could not post cinematic animation; using clean text loader")
        loader = await message.reply_text(
            "🎬 <b>CINEMA HUB OG</b>\n\n🔎 Preparing your experience…",
            parse_mode="HTML",
        )
        return loader, started


async def finish_cinematic_loader(loader, started, minimum=CINEMATIC_LOADER_MIN_SECONDS):
    """Keep loader visible long enough to be noticed, then remove it cleanly."""
    remaining = max(0.0, float(minimum) - (time.monotonic() - started))
    if remaining:
        await asyncio.sleep(remaining)
    try:
        await loader.delete()
    except Exception as exc:
        log.debug("Could not delete temporary cinematic loader: %s", exc)


async def send_request_prompt_after_delay(context, chat_id, user):
    cfg = context.application.bot_data["cfg"]
    await asyncio.sleep(cfg.start_delay_seconds)
    await context.bot.send_message(
        chat_id=chat_id,
        text=request_group_prompt(user),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 REQUEST HERE", url=request_group_url(cfg))]]),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

# ---------------------------
# Request/Search group
# ---------------------------

async def react_to_search_message(message):
    try:
        await message.set_reaction(reaction=random.choice(["🔥","❤️","😍","👍","🤩","⚡"]),is_big=True)
    except Exception:
        pass


def cinematic_search_stage(query, stage=0, premium=False, style_id=0):
    """Render one of several polished, single-message search animations."""
    q = escape(re.sub(r"\s+", " ", (query or "").strip())[:120])
    styles = [
        (
            ("🚀 <b>REQUEST RECEIVED.</b>", "Cinema HUB OG is warming up the database."),
            ("🎞️ <b>SCANNING THE DATABASE...</b>", "Checking titles, episodes and formats."),
            ("🔎 <b>MATCHING YOUR REQUEST...</b>", "Preparing your clean result set."),
        ),
        (
            ("🎬 <b>CINEMA HUB OG IS WORKING...</b>", "Serving your request from the indexed library."),
            ("🧭 <b>SEARCHING THE HUB...</b>", "Checking the closest title family and episodes."),
            ("✨ <b>RESULTS READY.</b>", "Finalizing your available files."),
        ),
        (
            ("⚡ <b>REQUEST LOCKED IN.</b>", "Your movie/series query is being processed."),
            ("🎞️ <b>CHECKING THE INDEX...</b>", "Looking through every matching episode/file."),
            ("🔎 <b>MATCH FOUND.</b>", "Building the result page now."),
        ),
        (
            ("🎬 <b>LIGHTS ON. SEARCH STARTED.</b>", "Cinema HUB OG is finding your request."),
            ("📚 <b>SEARCHING THE LIBRARY...</b>", "Comparing names, seasons and formats."),
            ("✅ <b>READY.</b>", "Your matching files are arriving."),
        ),
        (
            ("🌟 <b>YOUR REQUEST IS ON SCREEN.</b>", "The Hub is searching the full index."),
            ("🛰️ <b>DATABASE SCAN IN PROGRESS...</b>", "Checking the available source records."),
            ("🎯 <b>BEST MATCH FOUND.</b>", "Preparing the clean file list."),
        ),
        (
            ("🔔 <b>NEW SEARCH INCOMING.</b>", "Cinema HUB OG is taking care of it."),
            ("🔍 <b>DEEP SEARCH RUNNING...</b>", "Matching titles, episodes and quality info."),
            ("🎬 <b>MATCH FOUND.</b>", "Your result page is ready."),
        ),
        (
            ("🚦 <b>SEARCH ENGINE READY.</b>", "Checking the Cinema HUB OG database."),
            ("🎞️ <b>FILTERING THE INDEX...</b>", "Finding the most relevant files."),
            ("💫 <b>RESULTS LOADED.</b>", "Showing the files we found."),
        ),
        (
            ("🛎️ <b>REQUEST RECEIVED.</b>", "Let Cinema HUB OG do the searching."),
            ("🔎 <b>SCANNING EVERY MATCH...</b>", "Checking titles, language, season and quality."),
            ("🏁 <b>SEARCH COMPLETE.</b>", "Here are your available files."),
        ),
    ]
    style = styles[int(style_id) % len(styles)]
    lead, sub = style[min(stage, 2)]
    return (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "│   🎬 <b>CINEMA HUB OG</b>   │\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"<blockquote>{lead}\n{sub}</blockquote>\n\n"
        f"<blockquote>🔎 <b>Searching for :</b> <i>{q}</i></blockquote>"
    )


def cinematic_search_sequence():
    return ((0, 0.0), (1, 0.34), (2, 0.34))


async def _fetch_search_results(db, query, page_size):
    return await asyncio.gather(db.count_movies(query), db.find_movies(query, page_size))


async def group_search(update, context):
    message = update.message
    if not message or not message.text or (message.from_user and message.from_user.is_bot):
        return
    cfg = context.application.bot_data["cfg"]
    if str(update.effective_chat.id) != str(cfg.request_group):
        return
    query = message.text.strip()
    if not query or query.startswith("/") or not update.effective_user:
        return
    db = context.application.bot_data["db"]
    await react_to_search_message(message)
    try:
        await db.record_request(update.effective_user.id, query)
    except Exception:
        log.exception("Could not record search query")

    loader, loader_started = await show_cinematic_loader(
        message,
        f"🎬 <b>CINEMA HUB OG SEARCH</b>\nSearching for: <i>{escape(query[:100])}</i>",
    )
    search_task = asyncio.create_task(_fetch_search_results(db, query, cfg.page_size))
    try:
        total_results, results = await search_task
    except Exception:
        log.exception("Group search failed for query=%r", query)
        await finish_cinematic_loader(loader, loader_started)
        await message.reply_text(
            "⚠️ <b>CINEMA HUB OG SEARCH IS TEMPORARILY UNAVAILABLE.</b>\n\nPlease try again in a moment.",
            parse_mode="HTML",
            reply_to_message_id=message.message_id,
        )
        return
    await finish_cinematic_loader(loader, loader_started)
    searching = await message.reply_text(
        "✨ <b>RESULTS READY</b>\nPreparing your clean result page…",
        parse_mode="HTML",
        reply_to_message_id=message.message_id,
    )

    if not total_results:
        matches = await db.suggestion_matches(query, limit=3)
        sid = await db.create_search(
            update.effective_user.id, query, {"suggestions": [title for _, title in matches]},
            datetime.now(timezone.utc) + timedelta(minutes=30),
            request_chat_id=message.chat_id, request_message_id=message.message_id,
        )
        if matches and matches[0][0] >= 0.84:
            title = matches[0][1]
            total = await db.count_movies(title)
            result_page = await db.find_movies(title, cfg.page_size)
            await searching.edit_text(
                "✅ <b>MATCH FOUND</b>\n\n"
                f"<b>Closest match:</b> {escape(title)}",
                parse_mode="HTML",
            )
            await render_search_message(
                searching, context, result_page, sid, 0, title, update.effective_user.id, total
            )
            return
        if matches and matches[0][0] >= 0.64:
            buttons = [[InlineKeyboardButton(f"🎬 {title[:48]}", callback_data=f"suggest:{sid}:{i}")] for i, (_, title) in enumerate(matches)]
            suggestion_text = (
                "✨ <b>WE FOUND A CLOSE MATCH.</b>\n\n"
                "Choose the title that matches what you meant:"
            )
            await searching.edit_text(
                suggestion_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )
            await _persist_search_result_message(searching, db, sid, update.effective_user.id, cfg)
            return

        no_match_text = (
            "❌ <b>CINEMA HUB OG — NOT AVAILABLE RIGHT NOW</b>\n\n"
            f"<blockquote>{escape(query)}</blockquote>\n\n"
            "Unfortunately This Isn't Available On Our Database Right Now, Search Again After 10 Mins It Will Be Available for sure. Thank You ❤️"
        )
        await searching.edit_text(no_match_text, parse_mode="HTML")
        await _persist_search_result_message(searching, db, sid, update.effective_user.id, cfg)
        return

    await db.record_user(update.effective_user.id)
    search_id = await db.create_search(
        update.effective_user.id, query, {},
        datetime.now(timezone.utc) + timedelta(minutes=30),
        request_chat_id=message.chat_id, request_message_id=message.message_id,
    )
    await searching.edit_text("🔎 <b>MATCH FOUND</b>", parse_mode="HTML")
    await asyncio.sleep(0.18)
    await render_search_message(
        searching, context, results, search_id, 0, query, update.effective_user.id, total_results
    )


async def page_callback(update, context):
    q=update.callback_query
    try: _,page_raw,search_id=q.data.split(":",2); page=int(page_raw)
    except ValueError: await q.answer("Invalid page.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(search_id,q.from_user.id)
    if not doc: await q.answer("Search expired. Search again.",show_alert=True); return
    cfg=context.application.bot_data["cfg"]; filters_=doc.get("filters") or {}
    total_results=await db.count_movies(doc["query"],filters_)
    if not total_results: await q.answer("No results.",show_alert=True); return
    results=await db.find_movies(doc["query"],cfg.page_size,filters_,skip=page*cfg.page_size)
    if not results: await q.answer("Page expired. Try the last available page.",show_alert=True); return
    await q.answer(); await render_search_message(q.message,context,results,search_id,page,doc["query"],q.from_user.id,total_results)


async def filter_menu_callback(update, context):
    q=update.callback_query
    try: _,kind,search_id=q.data.split(":",2)
    except ValueError: await q.answer("Invalid filter.",show_alert=True); return
    if kind not in {"quality","language","season"}: await q.answer("Invalid filter.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(search_id,q.from_user.id)
    if not doc: await q.answer("Search expired.",show_alert=True); return
    values=await db.distinct_movie_values(doc["query"],kind)
    rows=[[InlineKeyboardButton(v[:38],callback_data=f"filter:{kind}:{search_id}:{i}")] for i,v in enumerate(values[:12])]
    rows.append([InlineKeyboardButton("‹ BACK TO RESULTS",callback_data=f"page:0:{search_id}")])
    await q.answer(); await q.message.edit_reply_markup(InlineKeyboardMarkup(rows))


async def filter_callback(update, context):
    q=update.callback_query
    try: _,kind,search_id,idx_raw=q.data.split(":",3); idx=int(idx_raw)
    except (ValueError,IndexError): await q.answer("Invalid filter.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(search_id,q.from_user.id)
    if not doc: await q.answer("Search expired.",show_alert=True); return
    values=await db.distinct_movie_values(doc["query"],kind)
    if idx<0 or idx>=len(values): await q.answer("Filter expired.",show_alert=True); return
    filters_=dict(doc.get("filters") or {}); filters_[kind]=values[idx]
    await db.update_search_filter(search_id,q.from_user.id,filters_)
    cfg=context.application.bot_data["cfg"]
    label = {"quality": "🎞 QUALITY", "language": "🌐 LANGUAGE", "season": "📺 SEASON"}.get(kind, "FILTER")
    try:
        await q.answer(f"{label} updating…")
        await q.message.edit_text(
            f"╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
            f"│ <b>{label} FILTER</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🎬 <b>REFINING YOUR RESULTS</b>\n"
            f"✨ <i>{escape(str(values[idx]))}</i>\n\n"
            "Checking the matching files…",
            parse_mode="HTML",
        )
        await asyncio.sleep(0.65)
    except Exception:
        pass
    total_results=await db.count_movies(doc["query"],filters_)
    if not total_results:
        await q.answer("No results for this filter.",show_alert=True)
        return
    results=await db.find_movies(doc["query"],cfg.page_size,filters_)
    await render_search_message(q.message,context,results,search_id,0,doc["query"],q.from_user.id,total_results)


async def suggest_callback(update, context):
    q=update.callback_query
    try: _,sid,idx_raw=q.data.split(":",2); idx=int(idx_raw)
    except (ValueError,IndexError): await q.answer("Invalid suggestion.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(sid,q.from_user.id); suggestions=(doc or {}).get("filters",{}).get("suggestions",[])
    if idx<0 or idx>=len(suggestions): await q.answer("Suggestion expired.",show_alert=True); return
    text=suggestions[idx]; cfg=context.application.bot_data["cfg"]
    total_results=await db.count_movies(text)
    if not total_results: await q.answer("No matching file found.",show_alert=True); return
    results=await db.find_movies(text,cfg.page_size)
    await q.answer("Found it ✅")
    await db.update_search_filter(sid,q.from_user.id,{})
    await render_search_message(
        q.message, context, results, sid, 0, text, q.from_user.id, total_results
    )

# ---------------------------
# Channel indexing
# ---------------------------

DB_CAPTION_PROVIDER_URL = "https://t.me/+CVrTuwZ-7l4xZmJl"
DB_CAPTION_SUPPORT_URL = "https://t.me/thevisionaryofficial"
DB_CAPTION_PROVIDER_TEXT = "CINEMA HUB ❤️🎬"
DB_CAPTION_SUPPORT_TEXT = "Ꮩɪꜱɪᴏɴᴀʀʏ ☆"
DB_CAPTION_MAX_UNITS = 1024

_DB_CAPTION_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)\S+|(?:t\.me|telegram\.me|telegram\.dog)/\S+|tg://\S+"
)
_DB_CAPTION_MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}")
_DB_CAPTION_MARKDOWN_LINK_RE = re.compile(
    r"(?i)\[([^\]]+)\]\((?:https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/)[^)]+\)"
)
_DB_CAPTION_PROMO_RE = re.compile(
    r"(?i)(?:\bpowered\s+by\b|\bprovided\s+by\b|\bshare\s*&\s*support\b|\bjoin\s+(?:our\s+|the\s+)?(?:telegram\s+)?channel\b|\bjoin\s+us\b|\bjoin\s+for\s+more\s+updates\b).*?$"
)
_DB_CAPTION_META_RE = re.compile(
    r"(?i)^\s*(?:🔊|🎧|💿|💬|🎵|🔉)?\s*(?:audio|languages?|language|quality|subs?|esubs?|msubs?|season)\s*:"
)
_DB_CAPTION_FOOTER_LINES = {
    f"➤ Provided By : {DB_CAPTION_PROVIDER_TEXT}",
    f"➤ Share & Support : {DB_CAPTION_SUPPORT_TEXT}",
}


def _telegram_utf16_units(text):
    return len(str(text).encode("utf-16-le")) // 2


def _truncate_telegram_text(text, max_units):
    text = str(text or "")
    if _telegram_utf16_units(text) <= max_units:
        return text
    suffix = "…"
    budget = max(0, max_units - _telegram_utf16_units(suffix))
    out = []
    used = 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > budget:
            break
        out.append(char)
        used += units
    return "".join(out).rstrip() + suffix


def clean_database_caption(raw_caption):
    """Return the operator's movie caption with source promotion/link noise removed.

    Telegram link/blockquote entities are intentionally not preserved: the returned
    text is re-sent as plain content and the final upper section is rendered bold.
    The visible text of a linked caption is kept; only the hyperlink behavior is
    discarded.
    """
    text = str(raw_caption or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # Remove literal quote prefixes. Telegram blockquote entities disappear
        # naturally when the caption is rebuilt below.
        line = re.sub(r"^\s*(?:>\s*)+", "", line)

        # Convert literal Markdown-style links to their visible label before
        # removing ordinary raw URLs. Telegram-native text links already arrive
        # here as their visible label, so their link behavior is also discarded.
        line = _DB_CAPTION_MARKDOWN_LINK_RE.sub(r"\1", line)
        line = _DB_CAPTION_URL_RE.sub("", line)
        line = _DB_CAPTION_MENTION_RE.sub("", line)

        # Remove source-credit/promotional content without touching the useful
        # movie title/quality/audio information that precedes it on the line.
        line = _DB_CAPTION_PROMO_RE.sub("", line)

        # Strip brackets left behind by removed Join/URL promotion, but keep
        # normal title brackets such as [Hindi HQ Dub].
        line = re.sub(r"\[\s*\]", "", line)
        line = re.sub(r"\(\s*\)", "", line)
        line = re.sub(r"\s{2,}", " ", line).strip()
        line = line.strip(" \t|•·")
        if line and not re.search(r"\w", line, re.UNICODE):
            continue
        if line:
            lines.append(line)

    # Give the first metadata line a single clean blank-line separator. This
    # preserves the compact movie title while making Audio/Quality/Subs blocks
    # much easier to scan.
    out=[]
    meta_gap_done=False
    for line in lines:
        if not meta_gap_done and _DB_CAPTION_META_RE.search(line) and out:
            out.append("")
            meta_gap_done=True
        out.append(line)
    return "\n".join(out).strip()


def _database_caption_html(upper_caption):
    footer_plain = (
        f"➤ Provided By : {DB_CAPTION_PROVIDER_TEXT}\n"
        f"➤ Share & Support : {DB_CAPTION_SUPPORT_TEXT}"
    )
    footer_units = _telegram_utf16_units(footer_plain)
    separator_units = _telegram_utf16_units("\n\n")
    upper_budget = max(1, DB_CAPTION_MAX_UNITS - footer_units - separator_units)
    upper_caption = _truncate_telegram_text(upper_caption, upper_budget)
    return (
    f"<b>{escape(upper_caption, quote=False)}</b>\n\n"
    f"<b>➤ Provided By : <a href=\"{escape(DB_CAPTION_PROVIDER_URL, quote=True)}\">"
    f"{escape(DB_CAPTION_PROVIDER_TEXT, quote=False)}</a></b>\n\n"
    f"<b>➤ Share &amp; Support : <a href=\"{escape(DB_CAPTION_SUPPORT_URL, quote=True)}\">"
    f"{escape(DB_CAPTION_SUPPORT_TEXT, quote=False)}</a></b>"
)


def _strip_our_database_footer(caption):
    """Remove only our own generated footer before indexing an edited post."""
    text = str(caption or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    kept = [line for line in lines if line not in _DB_CAPTION_FOOTER_LINES]
    return "\n".join(line for line in kept if line).strip()


def _new_database_caption(msg):
    """Build the new database-channel caption while leaving the media untouched."""
    source_caption = msg.caption or msg.text or ""
    upper = clean_database_caption(source_caption)
    if not upper:
        filename = clean_result_name(media_filename(msg) or f"File {msg.message_id}")
        upper = filename or f"File {msg.message_id}"
    return upper, _database_caption_html(upper)
async def index_channel_post(update, context):
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    if not cfg.auto_index:
        return
    if str(msg.chat.id) != str(cfg.database_channel):
        return
    if not (msg.video or msg.document or msg.audio or msg.photo):
        return

    is_new_post = bool(update.channel_post)
    try:
        if is_new_post:
            caption, caption_html = _new_database_caption(msg)
            doc = build_movie_document(
                msg,
                cfg.database_channel,
                caption_override=caption,
                caption_html=caption_html,
                caption_status="pending",
            )
        else:
            caption = clean_database_caption(_strip_our_database_footer(message_caption_text(msg)))
            if not caption:
                caption = clean_result_name(media_filename(msg) or f"File {message_id_value(msg)}")
            doc = build_movie_document(
                msg,
                cfg.database_channel,
                caption_override=caption,
                caption_html="",
                caption_status="done",
            )
        for attempt in range(1, 4):
            try:
                await db.upsert_movie(doc)
                break
            except Exception as exc:
                if attempt >= 3:
                    raise
                log.warning(
                    "Database index write failed (message=%s, attempt=%s): %s",
                    message_id_value(msg), attempt, exc,
                )
                await asyncio.sleep(attempt)
        log.info(
            "%s database-channel media: chat=%s message=%s title=%r",
            "Indexed live" if is_new_post else "Re-indexed edited",
            msg.chat.id,
            message_id_value(msg),
            doc["title"][:120],
        )
    except Exception:
        log.exception(
            "Failed to index database-channel media: chat=%s message=%s",
            msg.chat.id,
            message_id_value(msg),
        )


def _prepare_session_string(raw):
    """Normalize common Render/env formatting without inventing session data."""
    if not raw:
        return ""
    value = str(raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    value = "".join(value.split())
    # Telethon stores a one-character version prefix outside the base64 data.
    # Add omitted padding only to the base64 payload, never to the prefix.
    if value.startswith("1"):
        payload = value[1:]
        payload += "=" * (-len(payload) % 4)
        return "1" + payload
    return value


def _validate_session_string(raw):
    """Return a normalized StringSession or None if it is structurally invalid."""
    value = _prepare_session_string(raw)
    if not value:
        return None
    try:
        # Decode only for an early, deterministic format check. Telethon still
        # performs the authoritative StringSession parsing below. The first
        # character is Telethon's version prefix and is not part of base64.
        if not value.startswith("1"):
            raise ValueError("unsupported StringSession version")
        decoded = base64.urlsafe_b64decode(value[1:].encode("ascii"))
        if len(decoded) not in {263, 275}:
            raise ValueError(
                f"decoded session has unexpected length {len(decoded)}"
            )
        return value
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        log.warning("SESSION_STRING is invalid; historical indexing is disabled: %s", exc)
        return None



async def historical_index(cfg, db, force=False):
    """Resumable historical indexing of the configured Telegram database channel."""
    if not force and not cfg.index_on_start:
        return 0
    if not cfg.session_string:
        log.info("Historical indexing skipped: SESSION_STRING is not configured.")
        await db.set_index_state("historical", status="blocked", reason="SESSION_STRING missing", indexed=0)
        return 0
    if TelegramClient is None or StringSession is None:
        await db.set_index_state("historical", status="blocked", reason="Telethon unavailable", indexed=0)
        return 0
    session_string = _validate_session_string(cfg.session_string)
    if not session_string:
        await db.set_index_state("historical", status="blocked", reason="SESSION_STRING invalid", indexed=0)
        return 0

    total = 0
    source_seen = 0
    batch = []
    existing_ids = set(await db.movies.distinct("message_id", {"chat_id": cfg.database_channel}))
    seen_source_ids = set()
    started = datetime.now(timezone.utc)
    previous_state = await db.get_index_state("historical") or {}
    resume_before_id = None
    if not force and previous_state.get("status") in {"running", "error"} and previous_state.get("last_message_id"):
        resume_before_id = max(1, int(previous_state["last_message_id"]) - 1)
    await db.set_index_state(
        "historical",
        status="running",
        started_at=started,
        reason="",
        indexed=0,
        source_seen=0,
        resume_before_id=resume_before_id,
    )
    try:
        async with TelegramClient(StringSession(session_string), cfg.api_id, cfg.api_hash) as client:
            if not await client.is_user_authorized():
                await db.set_index_state("historical", status="blocked", reason="Telegram session unauthorized", indexed=0)
                return 0
            if resume_before_id:
                log.info(
                    "Resuming historical indexing for %s below message_id=%s",
                    cfg.database_channel,
                    resume_before_id,
                )
                iterator = client.iter_messages(cfg.database_channel, max_id=resume_before_id)
            else:
                log.info("Historical indexing started for %s", cfg.database_channel)
                iterator = client.iter_messages(cfg.database_channel)
            async for msg in iterator:
                if not (msg.video or msg.document or msg.audio or msg.photo):
                    continue
                source_seen += 1
                seen_source_ids.add(int(msg.id))
                caption = clean_database_caption(message_caption_text(msg))
                if not caption:
                    caption = clean_result_name(media_filename(msg) or f"File {msg.id}")
                doc = build_movie_document(
                    msg,
                    cfg.database_channel,
                    caption_override=caption,
                    caption_html="",
                    caption_status="done",
                )
                batch.append(doc)
                if len(batch) >= cfg.index_batch_size:
                    changed = await db.bulk_upsert_movies(batch)
                    total += len(batch)
                    last_id = int(batch[-1].get("message_id")) if batch else None
                    await db.set_index_state(
                        "historical",
                        status="running",
                        indexed=total,
                        source_seen=source_seen,
                        last_message_id=last_id,
                        progress_at=datetime.now(timezone.utc),
                    )
                    batch.clear()
            if batch:
                await db.bulk_upsert_movies(batch)
                total += len(batch)
                await db.set_index_state(
                    "historical",
                    status="running",
                    indexed=total,
                    source_seen=source_seen,
                    last_message_id=(int(batch[-1].get("message_id")) if batch else None),
                    progress_at=datetime.now(timezone.utc),
                )
            stale_ids = sorted(existing_ids - seen_source_ids)
            stale_deleted = 0
            if stale_ids:
                result = await db.movies.delete_many({
                    "chat_id": cfg.database_channel,
                    "message_id": {"$in": stale_ids},
                })
                stale_deleted = int(result.deleted_count)
                await db.invalidate_search_cache()
        await db.set_index_state(
            "historical",
            status="complete",
            indexed=total,
            source_seen=source_seen,
            completed_at=datetime.now(timezone.utc),
            last_message_id=None,
            stale_deleted=stale_deleted,
        )
        log.info("Historical index complete: %s media messages; stale deleted=%s.", total, stale_deleted)
        return total
    except Exception as exc:
        await db.set_index_state(
            "historical",
            status="error",
            indexed=total,
            source_seen=source_seen,
            reason=str(exc)[:500],
            failed_at=datetime.now(timezone.utc),
        )
        log.exception("Historical indexing failed after %s records.", total)
        return total



async def reconcile_index(cfg, db):
    """Treat Telegram as source-of-truth: restore missing records and delete stale Mongo records."""
    if not cfg.session_string:
        return {"status": "blocked", "reason": "SESSION_STRING missing"}
    session_string = _validate_session_string(cfg.session_string)
    if not session_string:
        return {"status": "blocked", "reason": "SESSION_STRING invalid"}
    existing = set(
        await db.movies.distinct("message_id", {"chat_id": cfg.database_channel})
    )
    seen_source_ids = set()
    source_count = 0
    missing = 0
    repaired = 0
    try:
        async with TelegramClient(StringSession(session_string), cfg.api_id, cfg.api_hash) as client:
            if not await client.is_user_authorized():
                return {"status": "blocked", "reason": "Telegram session unauthorized"}
            batch = []
            async for msg in client.iter_messages(cfg.database_channel):
                if not (msg.video or msg.document or msg.audio or msg.photo):
                    continue
                source_count += 1
                seen_source_ids.add(int(msg.id))
                if int(msg.id) in existing:
                    continue
                missing += 1
                caption = clean_database_caption(message_caption_text(msg))
                if not caption:
                    caption = clean_result_name(media_filename(msg) or f"File {msg.id}")
                batch.append(build_movie_document(msg, cfg.database_channel, caption_override=caption, caption_html="", caption_status="done"))
                if len(batch) >= cfg.index_batch_size:
                    await db.bulk_upsert_movies(batch)
                    repaired += len(batch)
                    batch.clear()
            if batch:
                await db.bulk_upsert_movies(batch)
                repaired += len(batch)
        stale_ids = sorted(existing - seen_source_ids)
        stale_deleted = 0
        if stale_ids:
            result = await db.movies.delete_many({
                "chat_id": cfg.database_channel,
                "message_id": {"$in": stale_ids},
            })
            stale_deleted = int(result.deleted_count)
            await db.invalidate_search_cache()
        indexed_count = await db.movies.count_documents({"chat_id": cfg.database_channel})
        await db.set_index_state(
            "reconcile",
            status="complete",
            source_count=source_count,
            indexed_count=indexed_count,
            missing_found=missing,
            repaired=repaired,
            stale_deleted=stale_deleted,
            completed_at=datetime.now(timezone.utc),
        )
        return {
            "status": "complete",
            "source_count": source_count,
            "indexed_count": indexed_count,
            "missing_found": missing,
            "repaired": repaired,
            "stale_deleted": stale_deleted,
        }
    except Exception as exc:
        await db.set_index_state("reconcile", status="error", reason=str(exc)[:500], failed_at=datetime.now(timezone.utc))
        return {"status": "error", "reason": str(exc)[:500]}

# ---------------------------
# Admin
# ---------------------------

def is_admin(update, cfg):
    return bool(
        update.effective_user
        and update.effective_user.id in cfg.admin_ids
    )


async def deny_admin(update, cfg):
    """Never silently swallow admin commands; show the exact ID to configure."""
    if not update.effective_message or not update.effective_user:
        return
    await update.effective_message.reply_text(
        "⛔ Admin access required.\n\n"
        f"Your Telegram ID: <code>{update.effective_user.id}</code>\n"
        "Add this numeric ID to Render → Environment → ADMIN_IDS, then redeploy.",
        parse_mode="HTML",
    )


async def id_cmd(update, context):
    if not update.effective_user or not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"🆔 Your Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def admin_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    await update.message.reply_text(
        "🛠 Admin:\n"
        "/stats\n"
        "/reindex (or /index)\n"
        "/index_status\n"
        "/reconcile\n"
        "/iasync\n"
        "/iastats\n"
        "/getsettings\n"
        "/broadcast <text>"
    )



async def stats_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    stats = await context.application.bot_data["db"].stats()
    await update.message.reply_text(
        "📊 <b>CINEMA HUB OG SYSTEM STATUS</b>\n\n"
        f"Database records: <b>{stats['movies']}</b>\n"
        f"Caption pending: <b>{stats['caption_pending']}</b>\n"
        f"Caption failed: <b>{stats['caption_failed']}</b>\n"
        f"Users: <b>{stats['users']}</b>\n"
        f"Requests: <b>{stats['requests']}</b>\n"
        f"Historical index: <b>{escape(str(stats['historical_status']))}</b>\n"
        f"Last indexed count: <b>{stats['historical_last_count']}</b>\n"
        f"Session configured: <b>{bool(cfg.session_string)}</b>\n\n"
        f"Auto-index new posts: <b>{cfg.auto_index}</b>",
        parse_mode="HTML",
    )



async def reindex_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    if not cfg.session_string:
        await update.message.reply_text(
            "⚠️ <b>Historical reindex requires SESSION_STRING.</b>\n\n"
            "Add a valid Telethon StringSession to the main bot's Render Environment.\n"
            "Do not use STREAM_SESSION_STRING here.",
            parse_mode="HTML",
        )
        return
    if _validate_session_string(cfg.session_string) is None:
        await update.message.reply_text(
            "⚠️ <b>SESSION_STRING is malformed.</b>\n\nGenerate a fresh valid Telethon StringSession and replace the main bot's value.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        "🔄 <b>Historical indexing started.</b>\n\n"
        "Telegram is treated as the source of truth.\n"
        "The indexer will upsert live media and remove stale Mongo records safely in batches.",
        parse_mode="HTML",
    )
    async def work():
        count = await historical_index(cfg, context.application.bot_data["db"], force=True)
        try:
            state = await context.application.bot_data["db"].get_index_state("historical") or {}
            await update.message.reply_text(
                f"✅ <b>Historical indexing finished.</b>\n\n"
                f"Indexed/upserted: <b>{count}</b> media records.\n"
                f"Stale Mongo records deleted: <b>{state.get('stale_deleted', 0)}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass
    asyncio.create_task(work())



async def index_status_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    db = context.application.bot_data["db"]
    stats = await db.stats()
    reconcile = await db.get_index_state("reconcile") or {}
    await update.message.reply_text(
        "╭━━━ <b>CINEMA HUB OG INDEX</b> ━━━╮\n"
        f"│ MongoDB records  : <b>{stats['movies']}</b>\n"
        f"│ Caption pending   : <b>{stats['caption_pending']}</b>\n"
        f"│ Historical       : <b>{escape(str(stats['historical_status']))}</b>\n"
        f"│ Last batch count  : <b>{stats['historical_last_count']}</b>\n"
        f"│ Reconcile status  : <b>{escape(str(reconcile.get('status', 'never')))}</b>\n"
        f"│ Source count      : <b>{reconcile.get('source_count', '—')}</b>\n"
        f"│ Missing found     : <b>{reconcile.get('missing_found', '—')}</b>\n"
        f"│ Repaired          : <b>{reconcile.get('repaired', '—')}</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯",
        parse_mode="HTML",
    )


async def reconcile_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    if not cfg.session_string:
        await update.message.reply_text(
            "⚠️ <b>Reconciliation requires SESSION_STRING.</b>",
            parse_mode="HTML",
        )
        return
    if _validate_session_string(cfg.session_string) is None:
        await update.message.reply_text(
            "⚠️ <b>SESSION_STRING is malformed.</b>",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        "🧭 <b>Database reconciliation started.</b>\n\n"
        "Telegram will be treated as the source of truth. Missing media will be restored and stale Mongo records will be removed.",
        parse_mode="HTML",
    )
    async def work():
        result = await reconcile_index(cfg, context.application.bot_data["db"])
        try:
            if result.get("status") == "complete":
                await update.message.reply_text(
                    "✅ <b>Reconciliation complete.</b>\n\n"
                    f"Telegram media: <b>{result['source_count']}</b>\n"
                    f"MongoDB media: <b>{result['indexed_count']}</b>\n"
                    f"Missing found: <b>{result['missing_found']}</b>\n"
                    f"Repaired: <b>{result['repaired']}</b>\n"
                    f"Stale Mongo records deleted: <b>{result.get('stale_deleted', 0)}</b>",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"⚠️ Reconciliation ended with status: <b>{escape(str(result.get('status')))}</b>\n{escape(str(result.get('reason', 'Unknown error')))}",
                    parse_mode="HTML",
                )
        except Exception:
            pass
    asyncio.create_task(work())

async def iasync_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    ingestor = context.application.bot_data.get("ingestor")
    if not ingestor or not cfg.ia_ingest_enabled:
        await update.message.reply_text(
            "⚠️ Automatic ingestion is disabled. Set IA_INGEST_ENABLED=true in Render and redeploy."
        )
        return
    result = await ingestor.sync_once(initial=False, max_items=cfg.ia_batch_size)
    reasons = result.get("skip_reasons") or {}
    reason_text = ""
    if reasons:
        reason_text = "\n\n<b>Skip reasons:</b>\n" + "\n".join(
            f"• {reason}: {count}" for reason, count in sorted(reasons.items())
        )
    await update.message.reply_text(
        "📥 <b>AUTHORIZED INGESTION</b>\n\n"
        f"Discovered: {result['discovered']}\n"
        f"Uploaded: {result['uploaded']}\n"
        f"Skipped: {result['skipped']}\n"
        f"Failed: {result['failed']}" + reason_text,
        parse_mode="HTML",
    )


async def iastats_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    ingestor = context.application.bot_data.get("ingestor")
    stats = await context.application.bot_data["db"].ingest_jobs.count_documents({})
    uploaded = await context.application.bot_data["db"].ingest_jobs.count_documents({"status": "uploaded"})
    failed = await context.application.bot_data["db"].ingest_jobs.count_documents({"status": "failed"})
    skipped = await context.application.bot_data["db"].ingest_jobs.count_documents({"status": "skipped"})
    await update.message.reply_text(
        "📊 <b>INGESTION STATUS</b>\n\n"
        f"Enabled: {cfg.ia_ingest_enabled}\n"
        f"Jobs: {stats}\n"
        f"Uploaded: {uploaded}\n"
        f"Skipped: {skipped}\n"
        f"Failed: {failed}\n"
        f"Worker running: {bool(ingestor and ingestor.running)}",
        parse_mode="HTML",
    )


async def settings_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    await update.message.reply_text(
        "⚙️ Settings\n"
        f"Force Subscribe: {cfg.require_fsub}\n"
        f"Softurl shortener: {cfg.require_shortlink}\n"
        f"Delete after: {cfg.delete_after}s\n"
        f"Search page size: {cfg.page_size}\n"
        f"Index on start: {cfg.index_on_start}\n"
        f"Auto ingestion: {cfg.ia_ingest_enabled}"
    )


async def broadcast_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "Usage: /broadcast <message>"
        )
        return

    cursor = context.application.bot_data["db"].users.find(
        {}, {"user_id": 1}
    )
    sent = 0
    async for row in cursor:
        try:
            await context.bot.send_message(row["user_id"], text)
            sent += 1
        except Exception:
            continue
    await update.message.reply_text(
        f"✅ Broadcast sent to {sent} users."
    )


# ---------------------------
# General updates
# ---------------------------

async def perform_premium_search(update, context, query):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    loader, loader_started = await show_cinematic_loader(
        update.effective_message,
        f"👑 <b>PREMIUM SEARCH</b>\nFinding: <i>{escape(query[:100])}</i>",
    )
    try:
        total_results, results = await _fetch_search_results(db, query, cfg.page_size)
    except Exception:
        log.exception("Premium search failed for query=%r", query)
        await finish_cinematic_loader(loader, loader_started)
        await update.effective_message.reply_text(
            "⚠️ <b>Premium search is temporarily unavailable.</b> Please try again shortly.",
            parse_mode="HTML",
        )
        return
    await finish_cinematic_loader(loader, loader_started)
    searching = await update.effective_message.reply_text(
        "✨ <b>PREMIUM RESULTS READY</b>\nPreparing your clean result page…",
        parse_mode="HTML",
    )
    if not total_results:
        matches = await db.suggestion_matches(query, limit=3)
        if matches and matches[0][0] >= 0.84:
            query2 = matches[0][1]
            total_results = await db.count_movies(query2)
            results = await db.find_movies(query2, cfg.page_size)
            query = query2
        elif matches and matches[0][0] >= 0.64:
            sid = await db.create_search(update.effective_user.id, query, {"suggestions": [t for _, t in matches]}, datetime.now(timezone.utc)+timedelta(minutes=30))
            buttons = [[InlineKeyboardButton(f"🎬 {title[:48]}", callback_data=f"suggest:{sid}:{i}")] for i, (_, title) in enumerate(matches)]
            await searching.edit_text("✨ <b>CLOSE MATCHES FOUND</b>\n\nChoose the title you meant:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            return
        else:
            await searching.edit_text(
                "❌ <b>NOT AVAILABLE IN THE CURRENT INDEX</b>\n\nSearch again after the next index refresh.", parse_mode="HTML"
            )
            return
    sid = await db.create_search(update.effective_user.id, query, {}, datetime.now(timezone.utc)+timedelta(minutes=30))
    await render_search_message(searching, context, results, sid, 0, query, update.effective_user.id, total_results)


async def private_text(update, context):
    if not update.message or not update.effective_user or update.effective_chat.type != "private":
        return
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    query = update.message.text.strip()
    if not query or query.startswith("/"):
        return

    premium = await db.is_premium(update.effective_user.id)
    if cfg.require_fsub and cfg.fsub_channels:
        missing = await missing_channels(context.bot, update.effective_user.id, cfg.fsub_channels)
        if missing:
            gate_id = new_token(10)
            context_data = {"type": "premium_search", "query": query} if premium else {"type": "free_search_redirect"}
            await db.create_fsub_gate(
                gate_id, update.effective_user.id, [],
                datetime.now(timezone.utc)+timedelta(minutes=30),
                context=context_data,
            )
            await update.message.reply_text(
                premium_fsub_text(),
                reply_markup=premium_fsub_keyboard(cfg, gate_id),
                parse_mode="HTML",
            )
            return

    if premium:
        await perform_premium_search(update, context, query)
        return

    await update.message.reply_text(
        request_group_prompt(update.effective_user),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 REQUEST HERE", url=request_group_url(cfg))]]),
        parse_mode="HTML",
    )


async def channel_type_dispatch(update, context):
    if update.channel_post or update.edited_channel_post:
        await index_channel_post(update, context)


async def noop_callback(update, context):
    query = update.callback_query
    if query:
        await query.answer()


async def menu_command(update, context, kind):
    if not update.effective_message:
        return
    if kind == "upgrade":
        await update.effective_message.reply_text(
            premium_plans_text(),
            reply_markup=premium_plans_keyboard(),
            parse_mode="HTML",
        )
        return
    if kind == "top":
        rows=await context.application.bot_data["db"].top_searches(10)
        text="⭐ <b>TOP SEARCHING</b>\n\n"+("\n".join(f"<b>{i}.</b> {escape(str(r.get('query') or 'Unknown'))} — <code>{r.get('count',0)}</code> searches" for i,r in enumerate(rows,1)) if rows else "No searches have been recorded yet.")
    else:
        text=menu_text(kind, context.application.bot_data["cfg"]) or "Unavailable."
    await update.effective_message.reply_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹ BACK TO MAIN MENU",callback_data="menu:main")]]),parse_mode="HTML")


async def error_handler(update, context):
    log.error(
        "Unhandled update exception: %s",
        context.error,
        exc_info=context.error,
    )
    cfg = context.application.bot_data.get("cfg")
    if cfg and cfg.log_chat_id:
        try:
            await context.bot.send_message(
                cfg.log_chat_id,
                f"⚠️ Bot error:\n{escape(str(context.error))[:3500]}",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def sweep_expired_premium(context):
    db = context.application.bot_data.get("db")
    cfg = context.application.bot_data.get("cfg")
    if not db or not cfg:
        return
    now = datetime.now(timezone.utc)
    try:
        cursor = db.users.find(
            {"premium_active": True, "premium_until": {"$lte": now}},
            {"user_id": 1, "premium_until": 1},
        ).limit(100)
        async for row in cursor:
            uid = int(row["user_id"])
            await db.users.update_one({"user_id": uid}, {"$set": {"premium_active": False, "premium_expired_at": now}})
            try:
                await context.bot.set_chat_member_tag(cfg.request_group, uid, tag=None)
            except Exception as exc:
                log.info("Could not clear premium group tag for %s: %s", uid, exc)
    except Exception:
        log.exception("Premium expiry sweep failed")

# ---------------------------
# Runtime / Render health
# ---------------------------

async def retry_telegram_call(label, fn, attempts=6):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except (BadRequest, Forbidden) as exc:
            # These are normally permanent configuration/access errors (for
            # example an invalid chat ID or a bot that cannot access a private
            # chat). Retrying the same request immediately only delays recovery.
            log.error("Telegram %s rejected permanently: %s", label, exc)
            raise
        except Exception as exc:
            last = exc
            log.warning(
                "Telegram %s failed (attempt %s/%s): %s",
                label, attempt, attempts, exc,
            )
            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 15))
    raise last

class Runtime:
    def __init__(self):
        self.cfg = Config()
        self.db = Database(self.cfg.mongo_uri, self.cfg.db_name)
        self.app = None
        self.telegram_task = None
        self.db_task = None
        self.db_ready = False
        self.telegram_ready = False
        self.index_started = False
        self.caption_task = None
        self.telethon_watch_task = None
        self.telethon_client = None
        self.caption_last_edit_at = 0.0
        self._stopping = False
        self.ingestor = InternetArchiveIngestor(self.cfg, self.db, log)
        self.premium = PremiumManager(self.db, self.cfg)
        self.cfg._runtime_db_ready = False
        self.cfg._runtime_telegram_ready = False


    async def _caption_worker(self):
        """Durably finish new database captions without blocking media indexing."""
        while not self._stopping:
            try:
                pending = await self.db.get_pending_captions(limit=10)
                if not pending:
                    await asyncio.sleep(self.cfg.caption_worker_interval)
                    continue
                if self.app is None:
                    await asyncio.sleep(1)
                    continue
                for doc in pending:
                    if self._stopping or self.app is None:
                        return
                    now = time.monotonic()
                    wait = self.cfg.caption_edit_min_interval - (now - self.caption_last_edit_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    attempts = int(doc.get("caption_attempts", 0) or 0)
                    try:
                        await self.app.bot.edit_message_caption(
                            chat_id=int(doc["chat_id"]),
                            message_id=int(doc["message_id"]),
                            caption=str(doc["caption_html"]),
                            parse_mode="HTML",
                        )
                        self.caption_last_edit_at = time.monotonic()
                        await self.db.mark_caption_done(doc["chat_id"], doc["message_id"])
                    except RetryAfter as exc:
                        retry_after = float(getattr(exc, "retry_after", 1.0) or 1.0)
                        self.caption_last_edit_at = time.monotonic()
                        await self.db.mark_caption_pending(
                            doc["chat_id"], doc["message_id"], attempts=attempts + 1,
                            error=f"RetryAfter: {retry_after:.1f}s",
                        )
                        await asyncio.sleep(max(1.0, retry_after) + 0.5)
                    except BadRequest as exc:
                        text = str(exc).lower()
                        if "message is not modified" in text:
                            await self.db.mark_caption_done(doc["chat_id"], doc["message_id"])
                        elif attempts >= 5:
                            await self.db.movies.update_one(
                                {"chat_id": int(doc["chat_id"]), "message_id": int(doc["message_id"])},
                                {"$set": {"caption_status": "failed", "caption_last_error": str(exc)[:500]}},
                            )
                        else:
                            await self.db.mark_caption_pending(
                                doc["chat_id"], doc["message_id"], attempts=attempts + 1,
                                error=str(exc),
                            )
                    except Exception as exc:
                        log.exception(
                            "Caption worker failed for chat=%s message=%s",
                            doc.get("chat_id"), doc.get("message_id"),
                        )
                        if attempts >= 5:
                            await self.db.movies.update_one(
                                {"chat_id": int(doc["chat_id"]), "message_id": int(doc["message_id"])},
                                {"$set": {"caption_status": "failed", "caption_last_error": str(exc)[:500]}},
                            )
                        else:
                            await self.db.mark_caption_pending(
                                doc["chat_id"], doc["message_id"], attempts=attempts + 1,
                                error=str(exc),
                            )
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Caption worker loop failed")
                await asyncio.sleep(max(2, self.cfg.caption_worker_interval))

    def _build_app(self):
        request = HTTPXRequest(
            connection_pool_size=64,
            read_timeout=self.cfg.telegram_read_timeout,
            write_timeout=self.cfg.telegram_write_timeout,
            connect_timeout=self.cfg.telegram_connect_timeout,
            pool_timeout=self.cfg.telegram_pool_timeout,
            http_version="1.1",
        )
        updates_request = HTTPXRequest(
            connection_pool_size=16,
            read_timeout=max(60, self.cfg.telegram_read_timeout),
            write_timeout=self.cfg.telegram_write_timeout,
            connect_timeout=self.cfg.telegram_connect_timeout,
            pool_timeout=self.cfg.telegram_pool_timeout,
            http_version="1.1",
        )
        app = (
            Application.builder()
            .token(self.cfg.bot_token)
            .request(request)
            .get_updates_request(updates_request)
            .concurrent_updates(True)
            .build()
        )
        self._register_handlers(app)
        return app

    def _register_handlers(self, app):
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("id", id_cmd))
        app.add_handler(CommandHandler("admin", admin_cmd))
        app.add_handler(CommandHandler("stats", stats_cmd))
        app.add_handler(CommandHandler("reindex", reindex_cmd))
        app.add_handler(CommandHandler("index", reindex_cmd))
        app.add_handler(CommandHandler("index_status", index_status_cmd))
        app.add_handler(CommandHandler("reconcile", reconcile_cmd))
        app.add_handler(CommandHandler("iasync", iasync_cmd))
        app.add_handler(CommandHandler("iastats", iastats_cmd))
        app.add_handler(CommandHandler("getsettings", settings_cmd))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))
        app.add_handler(CommandHandler("help", lambda update, context: menu_command(update, context, "help")))
        app.add_handler(CommandHandler("about", lambda update, context: menu_command(update, context, "about")))
        app.add_handler(CommandHandler("top", lambda update, context: menu_command(update, context, "top")))
        app.add_handler(CommandHandler("upgrade", lambda update, context: menu_command(update, context, "upgrade")))
        app.add_handler(CommandHandler("premium_add", lambda update, context: premium_admin_command(update, context, "add")))
        app.add_handler(CommandHandler("premium_remove", lambda update, context: premium_admin_command(update, context, "remove")))
        app.add_handler(CommandHandler("premium_status", lambda update, context: premium_admin_command(update, context, "status")))
        app.add_handler(CommandHandler("premium_list", lambda update, context: premium_admin_command(update, context, "list")))
        app.add_handler(CallbackQueryHandler(fsub_check, pattern=r"^fsub:"))
        app.add_handler(CallbackQueryHandler(payment_admin_callback, pattern=r"^payadmin:"))
        app.add_handler(CallbackQueryHandler(premium_callback, pattern=r"^premium:"))
        app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
        app.add_handler(CallbackQueryHandler(page_callback, pattern=r"^page:"))
        app.add_handler(CallbackQueryHandler(filter_menu_callback, pattern=r"^filter_menu:"))
        app.add_handler(CallbackQueryHandler(filter_callback, pattern=r"^filter:"))
        app.add_handler(CallbackQueryHandler(suggest_callback, pattern=r"^suggest:"))
        app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
        app.add_handler(TypeHandler(Update, channel_type_dispatch), group=1)
        app.add_handler(
            MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_search),
            group=0,
        )
        app.add_handler(
            MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text),
            group=0,
        )
        app.add_error_handler(error_handler)

    async def _cleanup_app(self):
        app = self.app
        self.app = None
        self.telegram_ready = False
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
        except Exception:
            log.exception("Telegram updater cleanup failed")
        try:
            if app.running:
                await app.stop()
        except Exception:
            log.exception("Telegram application cleanup failed")
        try:
            await app.shutdown()
        except Exception:
            log.exception("Telegram application shutdown failed")

    async def _resolve_and_validate(self, app):
        me = await retry_telegram_call(
            "get_me",
            lambda: app.bot.get_me(read_timeout=self.cfg.telegram_read_timeout),
            attempts=8,
        )
        actual_username = (me.username or "").lstrip("@")
        configured_username = (self.cfg.bot_username or "").lstrip("@")
        if configured_username and actual_username and configured_username != actual_username:
            log.warning(
                "BOT_USERNAME=%s does not match Telegram account @%s; using Telegram's actual username.",
                configured_username, actual_username,
            )
        self.cfg.bot_username = actual_username or configured_username
        if not self.cfg.bot_username:
            raise RuntimeError("Telegram did not provide a bot username and BOT_USERNAME is empty.")

        db_chat = await retry_telegram_call(
            "database channel lookup",
            lambda: app.bot.get_chat(self.cfg.database_channel),
        )
        request_chat = await retry_telegram_call(
            "request group lookup",
            lambda: app.bot.get_chat(self.cfg.request_group),
        )
        self.cfg.database_channel = db_chat.id
        self.cfg.request_group = request_chat.id

        # V4 fails closed: Force Subscribe is a product requirement, so a bad
        # channel configuration must not silently disable the gate.
        if self.cfg.require_fsub:
            if not self.cfg.fsub_channels:
                raise RuntimeError("REQUIRE_FSUB=true but FSUB_CHANNELS is empty.")
            valid_channels = []
            valid_links = []
            errors = []
            for idx, channel in enumerate(self.cfg.fsub_channels):
                try:
                    chat = await app.bot.get_chat(channel)
                except (BadRequest, Forbidden) as exc:
                    errors.append(f"{channel}: {exc}")
                    continue

                link = self.cfg.fsub_links[idx] if idx < len(self.cfg.fsub_links) else ""
                if not link:
                    username = getattr(chat, "username", None)
                    if username:
                        link = f"https://t.me/{username.lstrip('@')}"
                    else:
                        errors.append(f"{channel}: private channel requires FSUB_INVITE_LINKS")
                        continue

                valid_channels.append(chat.id)
                valid_links.append(link)

            if errors or not valid_channels or len(valid_channels) != len(self.cfg.fsub_channels):
                raise RuntimeError(
                    "Force Subscribe validation failed. Fix every FSUB channel before the bot can start: "
                    + " | ".join(errors or ["no usable channels"])
                )
            self.cfg.fsub_channels = valid_channels
            self.cfg.fsub_links = valid_links

        log.info(
            "Telegram configuration validated: @%s | DB=%s | Request=%s | FSub=%s",
            self.cfg.bot_username,
            self.cfg.database_channel,
            self.cfg.request_group,
            self.cfg.fsub_channels if self.cfg.require_fsub else "disabled",
        )

    async def _telegram_delete_watcher(self):
        """Keep MongoDB in lockstep with deletions from the Telegram DB channel."""
        if not self.cfg.session_string or TelegramClient is None or StringSession is None:
            log.warning("Deletion watcher disabled: SESSION_STRING/Telethon unavailable.")
            return
        session_string = _validate_session_string(self.cfg.session_string)
        if not session_string:
            log.warning("Deletion watcher disabled: SESSION_STRING is invalid.")
            return
        try:
            from telethon import events
            client = TelegramClient(StringSession(session_string), self.cfg.api_id, self.cfg.api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                log.warning("Deletion watcher disabled: Telegram session is not authorized.")
                await client.disconnect()
                return
            self.telethon_client = client

            @client.on(events.MessageDeleted(chats=self.cfg.database_channel))
            async def _deleted(event):
                try:
                    chat_id = getattr(event, "chat_id", None)
                    if chat_id is None or str(chat_id) != str(self.cfg.database_channel):
                        return
                    ids = [int(x) for x in (getattr(event, "deleted_ids", None) or [])]
                    if not ids:
                        return
                    result = await self.db.movies.delete_many({
                        "chat_id": int(self.cfg.database_channel),
                        "message_id": {"$in": ids},
                    })
                    if int(result.deleted_count):
                        await self.db.invalidate_search_cache()
                        log.info("Removed %s deleted Telegram DB media record(s): %s", result.deleted_count, ids)
                except Exception:
                    log.exception("Telegram deletion watcher failed")

            log.info("Telegram database deletion watcher is active for %s", self.cfg.database_channel)
            while not self._stopping:
                await asyncio.sleep(30)
                if not client.is_connected():
                    await client.connect()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Telegram deletion watcher stopped")
        finally:
            try:
                if getattr(self, "telethon_client", None) is not None:
                    await self.telethon_client.disconnect()
            except Exception:
                pass
            self.telethon_client = None

    async def _database_worker(self):
        delay = 3
        while not self._stopping:
            try:
                await self.db.init()
                self.db_ready = True
                self.cfg._runtime_db_ready = True
                log.info("MongoDB is ready.")
                asyncio.create_task(self.db._get_title_candidates(), name="warm-search-title-cache")
                asyncio.create_task(self.db.backfill_search_tokens(), name="backfill-search-tokens")
                return
            except Exception as exc:
                self.db_ready = False
                log.exception("MongoDB background initialization failed: %s", exc)
                if self._stopping:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _telegram_worker(self):
        delay = self.cfg.startup_retry_delay
        while not self._stopping:
            if not self.db_ready:
                await asyncio.sleep(2)
                continue
            try:
                app = self._build_app()
                self.app = app
                await app.initialize()
                await self._resolve_and_validate(app)
                app.bot_data["cfg"] = self.cfg
                app.bot_data["db"] = self.db
                app.bot_data["ingestor"] = self.ingestor
                app.bot_data["premium"] = self.premium
                await app.start()
                await self.premium.start()
                if self.telethon_watch_task is None or self.telethon_watch_task.done():
                    self.telethon_watch_task = asyncio.create_task(self._telegram_delete_watcher(), name="telegram-delete-watcher")
                if self.caption_task is None or self.caption_task.done():
                    self.caption_task = asyncio.create_task(self._caption_worker(), name="database-caption-worker")
                # Search-result messages are scheduled from MongoDB, so the
                # cleanup survives Render restarts instead of relying on a timer
                # that exists only in RAM. The job itself runs frequently enough
                # to keep deletion close to the requested 10-minute mark.
                app.job_queue.run_repeating(
                    cleanup_expired_search_messages,
                    interval=30,
                    first=5,
                    name="search-result-cleanup",
                )
                app.job_queue.run_repeating(
                    sweep_expired_premium,
                    interval=300,
                    first=30,
                    name="premium-expiry-sweep",
                )
                await app.updater.start_polling(
                    allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post"],
                    drop_pending_updates=False,
                )
                self.telegram_ready = True
                self.cfg._runtime_telegram_ready = True
                log.info("Telegram polling started successfully.")
                if self.cfg.ia_ingest_enabled:
                    await self.ingestor.start()
                if self.cfg.index_on_start and not self.index_started:
                    self.index_started = True
                    asyncio.create_task(historical_index(self.cfg, self.db))
                delay = self.cfg.startup_retry_delay
                while not self._stopping and self.app is app and app.updater.running:
                    await asyncio.sleep(10)
                if self._stopping:
                    return
                log.warning("Telegram polling stopped unexpectedly; reconnecting.")
                await self.ingestor.stop()
                self.cfg._runtime_telegram_ready = False
                await self._cleanup_app()
                await asyncio.sleep(delay)
                continue
            except Exception as exc:
                self.telegram_ready = False
                self.cfg._runtime_telegram_ready = False
                await self.ingestor.stop()
                log.exception("Telegram startup/connection attempt failed: %s", exc)
                await self._cleanup_app()
                if self._stopping:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def start(self):
        # Never block Render's HTTP server startup on external networks. Both
        # MongoDB and Telegram are retried in background workers.
        self.db_task = asyncio.create_task(self._database_worker())
        self.telegram_task = asyncio.create_task(self._telegram_worker())

    async def stop(self):
        self._stopping = True
        for task in (self.telegram_task, self.db_task, self.telethon_watch_task):
            if task:
                task.cancel()
        for task in (self.telegram_task, self.db_task, self.telethon_watch_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self.caption_task:
            self.caption_task.cancel()
            try:
                await self.caption_task
            except asyncio.CancelledError:
                pass
            self.caption_task = None
        await self.ingestor.stop()
        await self.premium.stop()
        self.cfg._runtime_db_ready = False
        self.cfg._runtime_telegram_ready = False
        await self._cleanup_app()
        try:
            await self.db.close()
        except Exception:
            log.exception("MongoDB shutdown failed")


async def create_api(runtime):
    @asynccontextmanager
    async def lifespan(api):
        await runtime.start()
        yield
        await runtime.stop()

    api = FastAPI(lifespan=lifespan)

    @api.get("/")
    async def root():
        return {
            "status": "ok",
            "service": "autofilter-movie-bot",
        }

    @api.get("/health")
    async def health():
        return {
            "status": "ok",
            "database_ready": runtime.db_ready,
            "telegram_ready": runtime.telegram_ready,
            "force_subscribe_configured": bool(runtime.cfg.require_fsub and runtime.cfg.fsub_channels),
            "payment_bot_configured": bool(runtime.cfg.payment_bot_token and runtime.cfg.payment_bot_username),
            "streaming_configured": bool(runtime.cfg.stream_base_url and runtime.cfg.stream_signing_secret),
        }

    return api


def run():
    async def runner():
        runtime = Runtime()
        api = await create_api(runtime)
        port = int(os.getenv("PORT", "10000"))
        server = uvicorn.Server(
            uvicorn.Config(
                api,
                host="0.0.0.0",
                port=port,
                log_level="info",
            )
        )
        await server.serve()

    asyncio.run(runner())


if __name__ == "__main__":
    run()
