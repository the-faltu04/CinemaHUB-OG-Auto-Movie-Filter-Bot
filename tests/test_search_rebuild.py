from pathlib import Path

from bot import derive_series_key, extract_episode_info, episode_query_info, Database

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
    assert extract_episode_info("Tuu Juliet Jatt Di Epi 189") == (None, 189)
    assert extract_episode_info("Tuu Juliet Jatt Di E189") == (None, 189)
    assert extract_episode_info("Tuu Juliet Jatt Di E 189") == (None, 189)
    assert extract_episode_info("Tuu Juliet Jatt Di Episode 189") == (None, 189)


def test_episode_query_info_normalizes_common_episode_spellings_to_one_stem():
    expected = ("tuu juliet jatt di", None, 189)
    for query in [
        "Tuu Juliet Jatt Di Epi 189",
        "Tuu Juliet Jatt Di Ep 189",
        "Tuu Juliet Jatt Di E189",
        "Tuu Juliet Jatt Di E 189",
        "Tuu Juliet Jatt Di Episode 189",
    ]:
        assert episode_query_info(query) == expected


def test_fuzzy_scorer_handles_common_movie_title_typos():
    cases = {
        "Kabri Singh": "kabir singh",
        "Kakli": "kalki",
        "Conjring": "the conjuring",
        "Avatr": "avatar",
    }
    titles = list(cases.values()) + ["tuu juliet jatt di", "the office"]
    for query, expected_title in cases.items():
        matches = Database._score_title_candidates(query, titles)
        assert matches and matches[0][1] == expected_title


def test_search_engine_no_longer_limits_series_to_title_candidate_cap():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    start = text.index("async def _resolve_search_criteria_and_count")
    end = text.index("async def distinct_movie_values", start)
    block = text[start:end]
    assert "_resolve_fuzzy_series_keys" in block
    assert '"title": {"$in": titles}' not in block
    assert '"search_tokens": {"$all": words}' in text
    assert "async def search_page" in block
    assert "allow_fuzzy" in block


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


def test_single_step_softurl_verification_and_twelve_hour_settings_present():
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
    assert '"VERIFICATION_ACCESS_TTL_SECONDS", 43200' in text


def test_request_group_ttl_and_concurrency_are_configurable():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert '"REQUEST_GROUP_SEARCH_DELETE_AFTER_SECONDS", 300' in text
    assert '"MAX_CONCURRENT_UPDATES", 16' in text
    assert ".concurrent_updates(self.cfg.max_concurrent_updates)" in text
    assert '"REQUEST_GROUP_SEARCH_DELETE_AFTER_SECONDS", 300' in text
    assert '"SEARCH_RESULT_DELETE_AFTER_SECONDS", 600' in text
    assert '"VERIFICATION_ACCESS_TTL_SECONDS", 43200' in text
    assert 'Your free verification session is active for <b>12 hours</b>.' in text
    assert 'free results for <b>12 hours</b>' in text
    assert 'async def create_query_batch' in text
    assert 'batch.get("mode") == "query"' in text


def test_free_users_get_locked_stream_button_and_plan_route():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert 'elif not is_premium_user:' in text
    assert 'InlineKeyboardButton("🎬 STREAM & DOWNLOAD", callback_data="stream_locked")' in text
    assert 'callback_data="premium:plans"' in text
