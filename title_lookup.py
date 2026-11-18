import asyncio
import re
import time
from difflib import SequenceMatcher
from urllib.parse import quote
import httpx

_CACHE = {}
_LOCK = asyncio.Lock()
_CACHE_TTL = 900

def _norm(value):
    return " ".join(re.sub(r"[^\w]+", " ", str(value or "").lower()).split())

async def _imdb(query, client):
    r = await client.get(f"https://v3.sg.media-imdb.com/suggestion/x/{quote(query)}.json", timeout=2.5)
    r.raise_for_status()
    data = r.json()
    target = _norm(query)
    if not data.get("d"):
        return False
    for item in data["d"][:10]:
        title = _norm(item.get("l"))
        qid = str(item.get("qid") or "")
        score = SequenceMatcher(None, target, title).ratio() if title else 0.0
        if title and qid in {"movie", "tvSeries", "tvMiniSeries", "tvEpisode", "videoGame"} and (target == title or target in title or title in target or score >= 0.82):
            return True
    return False

async def _tvmaze(query, client):
    r = await client.get("https://api.tvmaze.com/search/shows", params={"q": query}, timeout=2.5)
    r.raise_for_status()
    target = _norm(query)
    for item in r.json()[:10]:
        title = _norm((item.get("show") or {}).get("name"))
        score = SequenceMatcher(None, target, title).ratio() if title else 0.0
        if title and (target == title or target in title or title in target or score >= 0.82):
            return True
    return False

async def internet_title_exists(query):
    key = _norm(query)
    if not key:
        return False
    now = time.monotonic()
    if key in _CACHE and now - _CACHE[key][0] < _CACHE_TTL:
        return _CACHE[key][1]
    async with _LOCK:
        now = time.monotonic()
        if key in _CACHE and now - _CACHE[key][0] < _CACHE_TTL:
            return _CACHE[key][1]
        result = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5), follow_redirects=True, headers={"User-Agent": "CinemaHUBOG/3.0"}) as client:
                a, b = await asyncio.gather(_imdb(query, client), _tvmaze(query, client), return_exceptions=True)
                result = a is True or b is True
        except Exception:
            result = False
        _CACHE[key] = (time.monotonic(), result)
        return result
