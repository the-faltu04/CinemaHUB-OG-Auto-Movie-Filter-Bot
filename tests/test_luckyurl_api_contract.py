import ast
from pathlib import Path
from types import SimpleNamespace
import asyncio
import httpx

ROOT = Path(__file__).resolve().parents[1]


def _load_shortener_symbols():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LuckyURLShortenerError":
            wanted.append(node)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "shorten_luckyurl":
            wanted.append(node)
    module = ast.Module(body=wanted, type_ignores=[])
    namespace = {
        "httpx": httpx,
        "urlparse": __import__("urllib.parse", fromlist=["urlparse"]).urlparse,
        "_redact_shortener_detail": lambda value, *secrets: str(value),
    }
    exec(compile(module, str(ROOT / "bot.py"), "exec"), namespace)
    return namespace


class _FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.is_success = 200 <= status < 300
        self._payload = payload
        self.text = text or "{}"

    def json(self):
        return self._payload


class _FakeClient:
    response = None
    error = None
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params):
        self.calls.append((url, params))
        if self.error:
            raise self.error
        return self.response


def test_luckyurl_json_success_parses_shortened_url_and_sends_expected_parameters(monkeypatch):
    ns = _load_shortener_symbols()
    _FakeClient.calls = []
    _FakeClient.error = None
    _FakeClient.response = _FakeResponse(payload={
        "status": "success", "shortenedUrl": "https://luckyurl.in/abc123"
    })
    monkeypatch.setattr(ns["httpx"], "AsyncClient", _FakeClient)
    cfg = SimpleNamespace(
        luckyurl_api="dummy-test-token",
        luckyurl_base_url="https://luckyurl.in/api",
    )

    result = asyncio.run(ns["shorten_luckyurl"](cfg, "https://t.me/example_bot?start=test"))

    assert result == "https://luckyurl.in/abc123"
    assert len(_FakeClient.calls) == 1
    url, params = _FakeClient.calls[0]
    assert url == "https://luckyurl.in/api"
    assert params["api"] == "dummy-test-token"
    assert params["format"] == "json"
    assert params["url"].startswith("https://t.me/")


def test_luckyurl_transport_timeout_is_marked_uncertain_and_not_retried(monkeypatch):
    ns = _load_shortener_symbols()
    _FakeClient.calls = []
    _FakeClient.error = httpx.ReadTimeout("read timed out")
    monkeypatch.setattr(ns["httpx"], "AsyncClient", _FakeClient)
    cfg = SimpleNamespace(luckyurl_api="token", luckyurl_base_url="https://luckyurl.in/api")

    try:
        asyncio.run(ns["shorten_luckyurl"](cfg, "https://t.me/example_bot?start=test"))
    except ns["LuckyURLShortenerError"] as exc:
        assert exc.uncertain is True
    else:
        raise AssertionError("Expected a LuckyURLShortenerError")

    assert len(_FakeClient.calls) == 1



def test_luckyurl_provider_error_is_reported_without_marking_result_uncertain(monkeypatch):
    ns = _load_shortener_symbols()
    _FakeClient.calls = []
    _FakeClient.error = None
    _FakeClient.response = _FakeResponse(payload={
        "status": "error", "message": "Invalid API token"
    })
    monkeypatch.setattr(ns["httpx"], "AsyncClient", _FakeClient)
    cfg = SimpleNamespace(
        luckyurl_api="token",
        luckyurl_base_url="https://luckyurl.in/api",
    )
    try:
        asyncio.run(ns["shorten_luckyurl"](cfg, "https://t.me/example_bot?start=test"))
    except ns["LuckyURLShortenerError"] as exc:
        assert exc.uncertain is False
        assert "Invalid API token" in str(exc)
    else:
        raise AssertionError("Expected an LuckyURL provider error")
    assert len(_FakeClient.calls) == 1

def test_luckyurl_idempotently_reuses_saved_short_url(monkeypatch):
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = []
    names = {"LuckyURLShortenerError", "get_or_create_luckyurl_url"}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in names:
            wanted.append(node)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            wanted.append(node)
    namespace = {
        "asyncio": asyncio,
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "timedelta": __import__("datetime").timedelta,
        "create_deep_linked_url": lambda username, payload: f"https://t.me/{username}?start={payload}",
        "shorten_luckyurl": None,
        "_VERIFICATION_LINK_LOCKS": {},
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(ROOT / "bot.py"), "exec"), namespace)

    calls = []

    async def fake_shorten(cfg, destination):
        calls.append(destination)
        return "https://luckyurl.in/saved" 

    namespace["shorten_luckyurl"] = fake_shorten

    class FakeDB:
        def __init__(self):
            self.session = None

        async def get_active_verification_session(self, user_id, movie_ids, provider="luckyurl"):
            assert provider == "luckyurl"
            return self.session

        async def create_verification_session(self, user_id, movie_ids, expires_at, provider="luckyurl"):
            assert provider == "luckyurl"
            self.session = {
                "session_id": "session123",
                "shortener_status": "pending",
                "short_url": None,
            }
            return "session123"

        async def set_verification_shortener_state(self, session_id, status, short_url=None):
            self.session["shortener_status"] = status
            if short_url:
                self.session["short_url"] = short_url

    async def run_twice():
        db = FakeDB()
        cfg = SimpleNamespace(verification_session_ttl=1800)
        first = await namespace["get_or_create_luckyurl_url"](cfg, db, 77, ["file1"], "botname")
        second = await namespace["get_or_create_luckyurl_url"](cfg, db, 77, ["file1"], "botname")
        return first, second

    first, second = asyncio.run(run_twice())
    assert first == second == "https://luckyurl.in/saved"
    assert len(calls) == 1


def test_caption_cleaner_targets_promo_handles_and_preserves_credits_and_audio_metadata():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = []
    constants = {
        "_DB_CAPTION_URL_RE",
        "_DB_CAPTION_MARKDOWN_LINK_RE",
        "_DB_CAPTION_PROMO_RE",
        "_DB_CAPTION_META_RE",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants for target in node.targets
        ):
            wanted.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "clean_database_caption":
            wanted.append(node)
    namespace = {"re": __import__("re")}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(ROOT / "bot.py"), "exec"), namespace)

    cleaned = namespace["clean_database_caption"](
        "Movie Title 1080p WEB-DL @legitcredit primeXofficial\n"
        "🔊 Audio: Hindi, English\n"
        "Uploaded by @luffyautofilter\n"
        "Thanks @Aapkafreetv and @realcredit\n"
        "https://example.com"
    )

    assert "@legitcredit" in cleaned
    assert "@realcredit" in cleaned
    assert "primeXofficial" not in cleaned
    assert "luffyautofilter" not in cleaned
    assert "Aapkafreetv" not in cleaned
    assert "Movie Title 1080p WEB-DL @legitcredit\n\n🔊 Audio: Hindi, English" in cleaned
    assert "Uploaded by" not in cleaned
    assert "https://example.com" not in cleaned


def test_new_luckyurl_stage_and_existing_arlinks_callback_stage_are_both_supported():
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'parts[1] in {"luckyurl", "arlinks", "gplinks", "soft"}' in source
    assert 'if stage not in {"luckyurl", "arlinks", "gplinks", "soft"}:' in source
    assert '"luckyurl_verified": True' in source
    assert '"arlinks_verified": True' in source
