from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_python_files_compile():
    files = [ROOT / "bot.py", ROOT / "ingest.py", ROOT / "premium_system.py", ROOT / "title_lookup.py", ROOT / "stream_server" / "app.py"]
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"))


def test_repo_files_and_gitignore():
    for rel in ["requirements.txt", "bot.py", "stream_server/app.py", "stream_server/Dockerfile", "stream_server/requirements.txt"]:
        assert (ROOT / rel).exists(), rel
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "__pycache__/" in gi
    assert "*.py[cod]" in gi
    assert ".pytest_cache/" in gi
    assert ".env" in gi
    assert "\n*\n" not in gi


def test_stream_branding_and_controls():
    text = (ROOT / "stream_server/app.py").read_text(encoding="utf-8")
    for required in [
        "CINEMA HUB OG",
        "Your cinema. Instantly on screen.",
        "/download/",
        "vlc://",
        "com.mxtech.videoplayer.ad",
        "@APP.get(\"/media/{token}\")",
        "@APP.get(\"/hls/{token}/index.m3u8\")",
        "@APP.get(\"/compat/{token}\")",
        "org.videolan.vlc",
        "com.mxtech.videoplayer.ad",
        "intent://",
        "@APP.get(\"/health\")",
    ]:
        assert required in text, required


def test_brand_asset_and_stream_dependency():
    assert (ROOT / "stream_server" / "assets" / "cinema_hub_og_logo.jpg").exists()
    req = (ROOT / "stream_server" / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptg==0.6.0" in req


def test_search_optimization_present():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    for required in [
        "SEARCH_DB_CONCURRENCY",
        "search_tokens",
        "backfill_search_tokens",
        "_search_cache",
        "_search_db_semaphore",
    ]:
        assert required in text, required
