import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V4StaticTests(unittest.TestCase):
    def test_all_python_sources_parse(self):
        for path in [ROOT / "bot.py", ROOT / "premium_system.py", ROOT / "stream_server" / "app.py"]:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_request_url_locked(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        self.assertIn('return "https://t.me/moviesearchoffc"', text)
        self.assertIn('self.request_group = "@moviesearchoffc"', text)

    def test_free_delivery_has_locked_stream_button(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        marker = 'InlineKeyboardButton("🎬 STREAM & DOWNLOAD", url=stream_url)'
        self.assertIn(marker, text)
        self.assertIn('if is_premium_user and cfg.stream_base_url and cfg.stream_signing_secret:', text)
        self.assertIn('InlineKeyboardButton("🎬 STREAM & DOWNLOAD", callback_data="stream_locked")', text)
        self.assertIn("This feature is only for Premium users. Buy Premium to enjoy this feature", text)
        self.assertIn('InlineKeyboardButton("💎 VIEW PREMIUM PLANS", callback_data="premium:plans")', text)

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
