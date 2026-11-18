from pathlib import Path
import ast
from html import escape
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BOT_SOURCE = (ROOT / "bot.py").read_text(encoding="utf-8")
PREMIUM_SOURCE = (ROOT / "premium_system.py").read_text(encoding="utf-8")


def _top_level_nodes(source):
    tree = ast.parse(source)
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_missing_title_admin_message_is_html_safe_and_contains_request_context():
    nodes = _top_level_nodes(BOT_SOURCE)
    wanted = [nodes["_missing_title_admin_message"]]
    namespace = {"escape": escape}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(ROOT / "bot.py"), "exec"), namespace)

    user = SimpleNamespace(id=12345, first_name="A&B", last_name="User", username="kamal_test")
    chat = SimpleNamespace(id=-100123, title="Cinema & Requests", username=None)
    rendered = namespace["_missing_title_admin_message"](
        "Dune <Part Three>", user, chat, "Premium private search"
    )

    assert "Dune &lt;Part Three&gt;" in rendered
    assert "A&amp;B User" in rendered
    assert "@kamal_test" in rendered
    assert "12345" in rendered
    assert "Premium private search" in rendered
    assert "Cinema &amp; Requests" in rendered


def test_both_search_paths_forward_only_after_close_match_threshold_fails():
    nodes = _top_level_nodes(BOT_SOURCE)
    group = ast.get_source_segment(BOT_SOURCE, nodes["group_search"])
    premium = ast.get_source_segment(BOT_SOURCE, nodes["perform_premium_search"])
    assert "matches[0][0] >= CLOSE_MATCH_THRESHOLD" in group
    assert "matches[0][0] >= CLOSE_MATCH_THRESHOLD" in premium
    assert '"Request Group search"' in group
    assert '"Premium private search"' in premium
    assert "show_unindexed_title_result" in group
    assert "show_unindexed_title_result" in premium
    assert "CLOSE_MATCH_THRESHOLD = 0.60" in BOT_SOURCE


def test_user_notice_only_claims_admin_delivery_after_successful_forward():
    nodes = _top_level_nodes(BOT_SOURCE)
    source = ast.get_source_segment(BOT_SOURCE, nodes["show_unindexed_title_result"])
    assert "forwarded = await forward_missing_title_to_admins" in source
    assert "if forwarded:" in source
    assert "MISSING_TITLE_USER_MESSAGE" in source
    assert "admin notification could not be delivered yet" in source


def test_missing_title_recipients_fall_back_to_configured_admin_ids():
    assert 'env_first("MISSING_TITLE_ADMIN_IDS", "ADMIN_IDS", "ADMINS", default="")' in BOT_SOURCE
    helper = _top_level_nodes(BOT_SOURCE)["forward_missing_title_to_admins"]
    source = ast.get_source_segment(BOT_SOURCE, helper)
    assert 'getattr(cfg, "missing_title_admin_ids", set()) or getattr(cfg, "admin_ids", set())' in source
    assert "numeric Telegram user IDs" in source


def test_premium_never_enters_fsub_or_luckyurl_delivery_gate():
    nodes = _top_level_nodes(BOT_SOURCE)
    for name in ("begin_gate", "send_luckyurl"):
        source = ast.get_source_segment(BOT_SOURCE, nodes[name])
        assert "if is_premium_user:" in source or "if await db.is_premium(user_id):" in source
        assert "await deliver(update, context" in source
    verification = ast.get_source_segment(BOT_SOURCE, nodes["verification_return"])
    assert "if is_premium_user:" in verification
    assert "await deliver(update, context, session.get(\"movie_ids\") or [], direct=True)" in verification


def test_premium_plans_back_to_overview_and_overview_back_to_options():
    assert 'callback_data="premium:overview"' in PREMIUM_SOURCE
    assert 'callback_data="premium:overview"' in BOT_SOURCE
    assert 'callback_data="menu:options"' in BOT_SOURCE
    nodes = _top_level_nodes(BOT_SOURCE)
    menu = ast.get_source_segment(BOT_SOURCE, nodes["menu_callback"])
    premium = ast.get_source_segment(BOT_SOURCE, nodes["premium_callback"])
    assert 'if action == "options"' in menu
    assert 'if action == "upgrade"' in menu
    assert "premium_overview_text()" in menu
    assert 'if action == "overview"' in premium


def test_custom_animated_loader_asset_is_bundled():
    loader = ROOT / "stream_server" / "assets" / "cinema_hub_og_loader.gif"
    assert loader.exists()
    assert loader.stat().st_size > 10_000
    assert "CINEMATIC_LOADER_MIN_SECONDS = 1.8" in BOT_SOURCE


def test_admin_forwarding_sends_to_numeric_recipients_and_reports_partial_success():
    nodes = _top_level_nodes(BOT_SOURCE)
    wanted = [nodes["_missing_title_admin_message"], nodes["forward_missing_title_to_admins"]]

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            if kwargs["chat_id"] == 20:
                raise RuntimeError("admin has not opened the bot")
            return True

    class FakeButton:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class FakeMarkup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    import asyncio
    namespace = {
        "escape": escape,
        "log": SimpleNamespace(error=lambda *a, **k: None, exception=lambda *a, **k: None),
        "InlineKeyboardButton": FakeButton,
        "InlineKeyboardMarkup": FakeMarkup,
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(ROOT / "bot.py"), "exec"), namespace)

    user = SimpleNamespace(id=555, first_name="Test", last_name="User", username="tester")
    chat = SimpleNamespace(id=-100123, title="Requests", username=None)
    update = SimpleNamespace(effective_user=user, effective_chat=chat)
    bot = FakeBot()
    cfg = SimpleNamespace(missing_title_admin_ids={20, 10}, admin_ids=set())
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}), bot=bot)

    delivered = asyncio.run(namespace["forward_missing_title_to_admins"](
        update, context, "Unknown Film 2026", "Request Group search"
    ))

    assert delivered is True
    assert [item["chat_id"] for item in bot.sent] == [10, 20]
    assert "Unknown Film 2026" in bot.sent[0]["text"]
    assert bot.sent[0]["parse_mode"] == "HTML"


def test_admin_forwarding_does_not_claim_success_when_no_admin_ids_exist():
    nodes = _top_level_nodes(BOT_SOURCE)
    wanted = [nodes["_missing_title_admin_message"], nodes["forward_missing_title_to_admins"]]
    import asyncio
    namespace = {
        "escape": escape,
        "log": SimpleNamespace(error=lambda *a, **k: None, exception=lambda *a, **k: None),
        "InlineKeyboardButton": lambda *a, **k: None,
        "InlineKeyboardMarkup": lambda *a, **k: None,
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(ROOT / "bot.py"), "exec"), namespace)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=555, first_name="Test", last_name="", username=None),
        effective_chat=SimpleNamespace(id=-100123, title="Requests", username=None),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"cfg": SimpleNamespace(missing_title_admin_ids=set(), admin_ids=set())}),
        bot=SimpleNamespace(send_message=lambda **kwargs: None),
    )
    assert asyncio.run(namespace["forward_missing_title_to_admins"](
        update, context, "Unknown Film", "Request Group search"
    )) is False
