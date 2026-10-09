from pathlib import Path

from bot import derive_series_key, extract_episode_info

ROOT = Path(__file__).resolve().parents[1]


def test_series_key_collapses_episode_titles_to_one_family():
    assert derive_series_key(
        "Tuu Juliet Jatt Di S01E23 Nawab Heers Dreams in the Fire 360p JIOHS mp4"
    ) == "tuu juliet jatt di"
    assert derive_series_key(
        "Tuu Juliet Jatt Di S01E22 Buzzo Disguises as Nawab 720p JIOHS WEB mkv"
    ) == "tuu juliet jatt di"


def test_episode_info_extracts_season_and_episode():
    assert extract_episode_info("Tuu Juliet Jatt Di S01E23") == (1, 23)
    assert extract_episode_info("The Office Season 2 Episode 7 720p") == (2, 7)
    assert extract_episode_info("Some Show Ep 19") == (None, 19)


def test_search_engine_no_longer_limits_series_to_title_candidate_cap():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    start = text.index("async def count_movies")
    end = text.index("async def distinct_movie_values", start)
    block = text[start:end]
    assert "_resolve_fuzzy_series_keys" in block
    assert '"title": {"$in": titles}' not in block
    assert '"search_tokens": {"$all": words}' in text


def test_pending_caption_pipeline_is_durable_and_restart_safe():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert '"caption_status": "pending"' in text
    assert "get_pending_captions" in text
    assert "mark_caption_done" in text
    assert "drop_pending_updates=False" in text


def test_index_health_commands_are_registered():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("index_status", index_status_cmd)' in text
    assert 'CommandHandler("reconcile", reconcile_cmd)' in text
    assert "async def reconcile_index" in text


def test_single_step_softurl_verification_and_six_hour_settings_present():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    for required in [
        "shorten_softurl",
        "VERIFY SOFTURL",
        "verification_sessions",
        "verification_until",
        "VERIFICATION_ACCESS_TTL_SECONDS",
        "events.MessageDeleted(chats=self.cfg.database_channel)",
        "stale_deleted",
        "START_DELAY_SECONDS",
    ]:
        assert required in text, required
