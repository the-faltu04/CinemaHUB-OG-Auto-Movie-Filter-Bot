import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V4StaticTests(unittest.TestCase):
    def test_all_python_sources_parse(self):
        for path in [ROOT / "bot.py", ROOT / "premium_system.py", ROOT / "stream_server" / "app.py"]:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_request_group_uses_configured_chat_id_and_public_username(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('env_first("REQUEST_GROUP_ID")', text)
        self.assertIn('self.request_group_id', text)
        self.assertIn("return f\"https://t.me/{str(username).lstrip('@')}\"", text)

    def test_free_delivery_has_locked_stream_button(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        marker = 'InlineKeyboardButton("🎬 STREAM & DOWNLOAD", url=stream_url)'
        self.assertIn(marker, text)
        self.assertIn('if is_premium_user and cfg.stream_base_url and cfg.stream_signing_secret:', text)
        self.assertIn('InlineKeyboardButton("🎬 STREAM & DOWNLOAD", callback_data="stream_locked")', text)
        self.assertIn("This feature is only for Premium users. Buy Premium to enjoy this feature", text)
        self.assertIn('InlineKeyboardButton("💎 BUY PREMIUM PLAN", callback_data="premium:plans")', text)

    def test_send_all_batches_are_lazy_and_old_batches_remain_compatible(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        start = text.index("async def create_batch_for_search")
        end = text.index("async def _persist_search_result_message", start)
        create_block = text[start:end]
        self.assertIn("create_query_batch", create_block)
        self.assertNotIn("find_movie_ids", create_block)
        start_branch = text.index('if payload.startswith("all_")')
        end_branch = text.index('if payload.startswith("vs_")', start_branch)
        batch_block = text[start_branch:end_branch]
        self.assertIn('batch.get("movie_ids")', batch_block)
        self.assertIn('batch.get("query")', batch_block)
        self.assertIn("db.find_movie_ids", batch_block)

    def test_premium_bypasses_fsub_and_free_users_use_luckyurl(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('if not is_premium_user and cfg.require_fsub and cfg.fsub_channels:', text)
        self.assertIn('await send_luckyurl(update, context, clean_ids)', text)
        self.assertIn('VERIFY VIA ADS', text)
        self.assertIn('HOW TO VERIFY?', text)
        self.assertIn('CONTINUE WITHOUT VERIFICATION', text)
        self.assertIn('callback_data=f"premium:skip:{premium_action}"', text)
        skip = text[text.index('if action == "skip"'):text.index('async def premium_admin_command', text.index('if action == "skip"'))]
        self.assertIn('This feature is only for Premium users. Buy Premium to enjoy this feature', skip)
        self.assertNotIn('await db.consume_premium_action(token, q.from_user.id)', skip.split('if not await db.is_premium', 1)[1].split('action_doc = await db.consume_premium_action', 1)[0])
        self.assertIn('https://t.me/+WpwWsLHNVWA2ZmNl', (ROOT / '.env.example').read_text(encoding='utf-8'))

    def test_free_delivery_adds_styled_premium_nudge_after_five_minute_warning(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        delivery = text[text.index("async def deliver("):text.index("async def stream_locked_callback(")]
        self.assertLess(delivery.index("TEMPORARY DELIVERY"), delivery.index("KAB TAK FREE ME DEKHEGA BHAI"))
        self.assertIn('InlineKeyboardButton("💎 BUY PREMIUM PLAN", callback_data="premium:plans")', delivery)
        self.assertIn('sent_ids.append(premium_nudge.message_id)', delivery)
        self.assertNotIn("Kitna Free Me Dekhega", delivery)

    def test_request_group_reaction_has_small_transition_then_big_animation(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        start = text.index("async def react_to_search_message(")
        end = text.index("async def _record_request_background(", start)
        block = text[start:end]
        self.assertIn('reaction="👀", is_big=False', block)
        self.assertIn('is_big=True', block)
        self.assertIn('await asyncio.sleep(0.35)', block)

    def test_force_subscribe_is_fail_closed(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('raise RuntimeError("REQUIRE_FSUB=true but FSUB_CHANNELS is empty.")', text)
        self.assertIn('Force Subscribe validation failed.', text)

    def test_payment_and_stream_constants(self):
        p = (ROOT / "premium_system.py").read_text(encoding="utf-8")
        self.assertIn('"p3": {"label": "PLAN 3", "price": 99, "days": 90, "period": "3 Months"}', p)
        self.assertIn('PAY VIA UPI ONLY', p)
        self.assertIn('https://t.me/+7_i3pMzJBTFlZTQ1', (ROOT / ".env.example").read_text(encoding="utf-8"))

    def test_stream_service_has_premium_gate(self):
        text = (ROOT / "stream_server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('Premium access required', text)
        self.assertIn('@APP.get("/watch/{token}"', text)
        self.assertIn('@APP.get("/download/{token}")', text)


if __name__ == "__main__":
    unittest.main()
