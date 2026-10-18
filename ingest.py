"""Authorized/public-domain media ingestion for Cinema HUB OG.

This module only consumes sources explicitly configured by the operator. The
built-in adapter targets Internet Archive items that advertise public-domain
or permissive non-NC Creative Commons licensing in their metadata.
"""

import asyncio
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:  # pragma: no cover
    TelegramClient = None
    StringSession = None


IA_API = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/{identifier}"
IA_DOWNLOAD = "https://archive.org/download/{identifier}/{filename}"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".ogv", ".mov", ".avi", ".m4v"}
SKIP_NAME_RE = re.compile(r"(?i)(?:_thumb|_sample|_preview|_meta|_orig|\.srt$|\.vtt$)")


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _license_kind(*values):
    """Return a conservative license classification from IA metadata.

    The search query is intentionally broad; this function is the final gate.
    It accepts explicit Public Domain / CC BY / CC BY-SA / CC BY-ND metadata
    and rejects NC, unknown, or missing licensing claims.
    """
    text = " ".join(str(v or "") for v in values).lower()
    if not text:
        return None
    if "by-nc" in text or "noncommercial" in text or "non-commercial" in text:
        return None
    if "creativecommons.org/publicdomain/mark/1.0" in text or "creativecommons.org/publicdomain/zero/1.0" in text:
        return "public-domain"
    if "public domain" in text or "publicdomain" in text:
        return "public-domain"
    if "creativecommons.org/licenses/by-sa/" in text or re.search(r"\bcc\s*by[- ]sa\b", text):
        return "cc-by-sa"
    if "creativecommons.org/licenses/by-nd/" in text or re.search(r"\bcc\s*by[- ]nd\b", text):
        return "cc-by-nd"
    if "creativecommons.org/licenses/by/" in text or re.search(r"\bcc\s*by\b", text):
        return "cc-by"
    return None


class InternetArchiveIngestor:
    """Poll Internet Archive, import newly discovered licensed movie files,
    and place them into the configured Telegram database channel.
    """

    def __init__(self, cfg, db, logger):
        self.cfg = cfg
        self.db = db
        self.log = logger
        self.task: Optional[asyncio.Task] = None
        self.stop_event = asyncio.Event()
        self.client = None
        self.running = False
        self.last_sync = None
        self.last_result = {"discovered": 0, "uploaded": 0, "skipped": 0, "failed": 0, "skip_reasons": {}}
        self._sync_lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(self.cfg.ia_ingest_enabled)

    async def start(self):
        if not self.enabled or self.task:
            return
        self.stop_event.clear()
        self.task = asyncio.create_task(self._loop(), name="internet-archive-ingestor")
        self.log.info("Internet Archive ingestion worker started.")

    async def stop(self):
        self.stop_event.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                self.log.exception("Internet Archive Telethon client shutdown failed")
            self.client = None
        self.running = False

    async def _loop(self):
        # Wait until Telegram + Mongo are ready before importing anything.
        while not self.stop_event.is_set() and not (self.db_ready and self.telegram_ready):
            await asyncio.sleep(5)
        if self.stop_event.is_set():
            return

        if self.cfg.ia_initial_backfill:
            try:
                await self.sync_once(initial=True)
            except Exception:
                self.log.exception("Initial Internet Archive sync failed")

        while not self.stop_event.is_set():
            try:
                await self.sync_once(initial=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("Internet Archive sync failed")
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.cfg.ia_scan_interval,
                )
            except asyncio.TimeoutError:
                pass

    @property
    def db_ready(self):
        return getattr(self.cfg, "_runtime_db_ready", False)

    @property
    def telegram_ready(self):
        return getattr(self.cfg, "_runtime_telegram_ready", False)

    async def sync_once(self, initial=False, max_items=None):
        if not self.enabled:
            return {"discovered": 0, "uploaded": 0, "skipped": 0, "failed": 0}
        if self._sync_lock.locked():
            return {"discovered": 0, "uploaded": 0, "skipped": 0, "failed": 0}

        async with self._sync_lock:
            self.running = True
            result = {"discovered": 0, "uploaded": 0, "skipped": 0, "failed": 0, "skip_reasons": {}}
            self.last_sync = datetime.now(timezone.utc)
            self.last_result = result
            limit = max_items or (self.cfg.ia_initial_limit if initial else self.cfg.ia_batch_size)
            pages = self.cfg.ia_initial_pages if initial else self.cfg.ia_scan_pages

            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.cfg.ia_http_timeout, connect=20),
                    follow_redirects=True,
                    headers={"User-Agent": "Cinema-HUB-OG-Authorized-Ingest/1.0"},
                ) as http:
                    for page in range(1, pages + 1):
                        docs = await self._search(http, page, self.cfg.ia_page_size)
                        if not docs:
                            break
                        for doc in docs:
                            # Scan multiple IA pages until we actually find the
                            # requested number of eligible uploads. This avoids
                            # getting stuck on the first page full of unlicensed items.
                            result["discovered"] += 1
                            try:
                                status = await self._process_item(http, doc)
                                result[status] += 1
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                result["failed"] += 1
                                self.log.exception(
                                    "Internet Archive item failed: %s (%s)",
                                    doc.get("identifier"),
                                    exc,
                                )
                        if result["uploaded"] >= limit:
                            break
            finally:
                self.running = False
                self.last_result = result
                self.log.info("Internet Archive sync complete: %s", result)
            return result

    async def _search(self, http, page, rows):
        params = [
            ("q", self.cfg.ia_query),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "description"),
            ("fl[]", "creator"),
            ("fl[]", "date"),
            ("fl[]", "year"),
            ("fl[]", "licenseurl"),
            ("fl[]", "publicdate"),
            ("rows", str(rows)),
            ("page", str(page)),
            ("output", "json"),
            ("sort[]", "publicdate desc"),
        ]
        response = await http.get(IA_API, params=params)
        response.raise_for_status()
        data = response.json()
        return (data.get("response") or {}).get("docs") or []

    async def _job(self, identifier):
        return await self.db.ingest_jobs.find_one(
            {"source": "internet_archive", "identifier": identifier}
        )

    async def _mark(self, identifier, **fields):
        fields["updated_at"] = datetime.now(timezone.utc)
        await self.db.ingest_jobs.update_one(
            {"source": "internet_archive", "identifier": identifier},
            {"$set": fields, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    def _record_skip(self, result, identifier, reason):
        reasons = result.setdefault("skip_reasons", {})
        reasons[reason] = reasons.get(reason, 0) + 1
        self.log.warning("Internet Archive item skipped: %s | reason=%s", identifier, reason)

    async def _process_item(self, http, doc):
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier:
            self.log.warning("Internet Archive item skipped: missing identifier")
            return "skipped"

        existing = await self._job(identifier)
        if existing and existing.get("status") == "uploaded":
            self.log.info("Internet Archive item skipped: %s | reason=already-uploaded", identifier)
            return "skipped"
        if existing and existing.get("status") == "skipped" and not self.cfg.ia_retry_skipped:
            self.log.info("Internet Archive item skipped: %s | reason=previously-skipped-and-retry-disabled", identifier)
            return "skipped"

        await self._mark(identifier, status="processing", title=doc.get("title"))
        metadata_url = IA_METADATA.format(identifier=quote(identifier, safe=""))
        response = await http.get(metadata_url)
        response.raise_for_status()
        item = response.json()
        md = item.get("metadata") or {}
        license_kind = _license_kind(md.get("licenseurl"), md.get("license"), md.get("rights"), doc.get("licenseurl"))

        if license_kind is None:
            reason = "no-supported-license"
            await self._mark(identifier, status="skipped", reason=reason)
            self.log.warning("Internet Archive item skipped: %s | reason=%s | licenseurl=%r license=%r rights=%r", identifier, reason, md.get("licenseurl"), md.get("license"), md.get("rights"))
            return "skipped"

        file_info = self._choose_file(item.get("files") or [])
        if not file_info:
            reason = "no-supported-video-under-size-limit"
            await self._mark(identifier, status="skipped", reason=reason)
            self.log.warning("Internet Archive item skipped: %s | reason=%s | max_file_mb=%s | file_count=%s", identifier, reason, self.cfg.ia_max_file_mb, len(item.get("files") or []))
            return "skipped"

        filename = str(file_info.get("name"))
        size = _safe_int(file_info.get("size"), 0)
        download_url = IA_DOWNLOAD.format(
            identifier=quote(identifier, safe=""),
            filename=quote(filename, safe="/"),
        )
        title = str(md.get("title") or doc.get("title") or identifier).strip()
        year = md.get("year") or md.get("date") or doc.get("year") or doc.get("date")
        creator = md.get("creator") or doc.get("creator")
        description = md.get("description") or doc.get("description") or ""
        caption = self._caption(title, year, creator, license_kind, identifier)

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="cinema_ia_", suffix=Path(filename).suffix, delete=False) as tmp:
                temp_path = tmp.name
                downloaded = 0
                async with http.stream("GET", download_url) as stream:
                    stream.raise_for_status()
                    length = _safe_int(stream.headers.get("Content-Length"), size)
                    if length and length > self.cfg.ia_max_file_mb * 1024 * 1024:
                        reason = "file-too-large"
                        await self._mark(identifier, status="skipped", reason=reason, size=length)
                        self.log.warning("Internet Archive item skipped: %s | reason=%s | size_bytes=%s | max_file_mb=%s", identifier, reason, length, self.cfg.ia_max_file_mb)
                        return "skipped"
                    async for chunk in stream.aiter_bytes(1024 * 1024):
                        downloaded += len(chunk)
                        if downloaded > self.cfg.ia_max_file_mb * 1024 * 1024:
                            reason = "file-too-large"
                            await self._mark(identifier, status="skipped", reason=reason, size=downloaded)
                            self.log.warning("Internet Archive item skipped: %s | reason=%s | downloaded_bytes=%s | max_file_mb=%s", identifier, reason, downloaded, self.cfg.ia_max_file_mb)
                            return "skipped"
                        tmp.write(chunk)

            message = await self._upload(temp_path, caption, filename)
            message_id = getattr(message, "id", None)
            if not message_id:
                raise RuntimeError("Telegram upload returned no message ID")

            parsed = self._parse_source_metadata(title, filename, year, md)
            await self.db.upsert_movie(
                {
                    "chat_id": self.cfg.database_channel,
                    "message_id": int(message_id),
                    "title": parsed["title"][:500],
                    "caption": caption[:4000],
                    "filename": filename,
                    "normalized": normalize(f"{parsed['title']} {filename} {description}"),
                    "quality": parsed["quality"],
                    "quality_rank": parsed["quality_rank"],
                    "language": parsed["language"],
                    "season": parsed["season"],
                    "size": parsed["size"],
                    "source": "internet_archive",
                    "source_identifier": identifier,
                    "license": license_kind,
                    "source_url": f"https://archive.org/details/{quote(identifier, safe='')}",
                    "indexed_at": datetime.now(timezone.utc),
                }
            )
            await self._mark(
                identifier,
                status="uploaded",
                message_id=int(message_id),
                filename=filename,
                size=size,
                license=license_kind,
                source_url=f"https://archive.org/details/{quote(identifier, safe='')}",
            )
            return "uploaded"
        except Exception as exc:
            await self._mark(identifier, status="failed", reason=str(exc)[:500])
            raise
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _choose_file(self, files):
        candidates = []
        for info in files:
            name = str(info.get("name") or "")
            suffix = Path(name).suffix.lower()
            if suffix not in VIDEO_EXTENSIONS or SKIP_NAME_RE.search(name):
                continue
            size = _safe_int(info.get("size"), 0)
            if size <= 0 or size > self.cfg.ia_max_file_mb * 1024 * 1024:
                continue
            fmt = str(info.get("format") or "").lower()
            score = 0
            if suffix == ".mp4":
                score += 100
            if "mpeg4" in fmt or "h.264" in fmt or "h264" in fmt:
                score += 20
            # Prefer the largest suitable master-ish file; tiny previews are
            # already filtered by the filename rules and the size floor below.
            score += min(size / (1024 * 1024), 200)
            candidates.append((score, size, info))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][2]

    async def _upload(self, path, caption, filename):
        if TelegramClient is None or StringSession is None:
            raise RuntimeError("Telethon is not installed")
        session = str(self.cfg.session_string or "").strip()
        if not session:
            raise RuntimeError("SESSION_STRING is required for automatic Telegram uploads")
        # Normalize common Render/env formatting exactly like the main bot
        # does for historical indexing. Telethon StringSession contains a
        # one-character version prefix followed by URL-safe base64; some env
        # stores/secret pastes omit the trailing '=' padding.
        if len(session) >= 2 and session[0] == session[-1] and session[0] in {"'", '"'}:
            session = session[1:-1].strip()
        session = "".join(session.split())
        if session.startswith("1"):
            payload = session[1:]
            payload += "=" * (-len(payload) % 4)
            session = "1" + payload
        if self.client is None:
            self.client = TelegramClient(
                StringSession(session),
                self.cfg.api_id,
                self.cfg.api_hash,
                connection_retries=5,
                retry_delay=3,
            )
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError("SESSION_STRING is not authorized")

        return await self.client.send_file(
            self.cfg.database_channel,
            path,
            caption=caption,
            supports_streaming=Path(filename).suffix.lower() in {".mp4", ".m4v", ".mov"},
            force_document=False,
        )

    def _caption(self, title, year, creator, license_kind, identifier):
        parts = [f"🎬 {title}"]
        if year:
            parts.append(f"📅 {year}")
        if creator:
            parts.append(f"🎥 {creator}")
        parts.append(f"⚖️ License: {license_kind}")
        parts.append(f"🔗 archive.org/details/{identifier}")
        return "\n".join(parts)[:1024]

    def _parse_source_metadata(self, title, filename, year, md):
        # Import lazily so ingest.py remains independent from bot startup code.
        text = f"{title} {filename} {year or ''} {md.get('language') or ''}"
        q = re.search(r"(?i)\b(4k|2160p|1440p|2k|1080p|720p|480p|360p)\b", text)
        s = re.search(r"(?i)\b(?:season|s)\s*(\d{1,2})\b", text)
        l = re.search(r"(?i)\b(hindi|english|tamil|telugu|malayalam|kannada|punjabi|bengali|dual audio|multi audio)\b", text)
        z = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(gb|mb)\b", text)
        quality = q.group(1).upper() if q else None
        ranks = {"4K": 0, "2160P": 0, "1440P": 1, "2K": 1, "1080P": 2, "720P": 3, "480P": 4, "360P": 5}
        return {
            "title": title,
            "quality": quality,
            "quality_rank": ranks.get(quality, 99),
            "language": l.group(1).title() if l else md.get("language"),
            "season": f"Season {s.group(1)}" if s else None,
            "size": z.group(0).upper() if z else None,
        }
