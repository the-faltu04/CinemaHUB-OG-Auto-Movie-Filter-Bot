import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from bot import Database, cleanup_expired_search_messages, create_batch_for_search


class SearchPerformanceTests(unittest.IsolatedAsyncioTestCase):
    def test_episode_queries_target_only_the_requested_episode(self):
        db = object.__new__(Database)
        db._search_tokens_ready = True
        for query in (
            "Tuu Juliet Jatt Di Epi 189",
            "Tuu Juliet Jatt Di Ep 189",
            "Tuu Juliet Jatt Di E189",
            "Tuu Juliet Jatt Di E 189",
            "Tuu Juliet Jatt Di Episode 189",
        ):
            self.assertEqual(
                db._exact_search_criteria(query),
                {"series_key": "tuu juliet jatt di", "episode_number": 189},
            )

    async def test_search_render_creates_lazy_batch_without_fetching_movie_ids(self):
        class FakeDB:
            def __init__(self):
                self.created = None

            async def create_query_batch(self, *args):
                self.created = args

        fake_db = FakeDB()
        cfg = SimpleNamespace(send_all_limit=1000)
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"db": fake_db, "cfg": cfg}))

        batch_id = await create_batch_for_search(
            context,
            user_id=123,
            query="Tuu Juliet Jatt Di E189",
            filters_={"quality": "720P"},
            limit=1000,
        )

        self.assertTrue(batch_id)
        self.assertIsNotNone(fake_db.created)
        self.assertEqual(fake_db.created[1], 123)
        self.assertEqual(fake_db.created[2], "Tuu Juliet Jatt Di E189")
        self.assertEqual(fake_db.created[3], {"quality": "720P"})
        self.assertEqual(fake_db.created[4], 1000)
        # FakeDB deliberately has no find_movie_ids method: rendering must not
        # materialize up to 1,000 IDs before the user taps SEND ALL.

    async def test_cleanup_respects_separate_request_and_result_expiries(self):
        now = datetime.now(timezone.utc)

        class FakeDB:
            def __init__(self, docs):
                self.docs = docs
                self.deleted_requests = []
                self.deleted_results = []

            async def expired_search_messages(self, limit=100):
                return self.docs

            async def mark_search_request_deleted(self, sid):
                self.deleted_requests.append(sid)

            async def mark_search_result_deleted(self, sid):
                self.deleted_results.append(sid)

        class FakeBot:
            def __init__(self):
                self.deleted = []

            async def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))

        docs = [
            {
                "search_id": "request-not-yet-due",
                "request_chat_id": -100123,
                "request_message_id": 1,
                # Simulate PyMongo's default naive-UTC datetime results.
                "request_expires_at": (now + timedelta(minutes=1)).replace(tzinfo=None),
                "result_chat_id": -100123,
                "result_message_id": 2,
                "result_expires_at": (now - timedelta(seconds=1)).replace(tzinfo=None),
            },
            {
                "search_id": "result-not-yet-due",
                "request_chat_id": -100123,
                "request_message_id": 3,
                "request_expires_at": (now - timedelta(seconds=1)).replace(tzinfo=None),
                "result_chat_id": -100123,
                "result_message_id": 4,
                "result_expires_at": (now + timedelta(minutes=1)).replace(tzinfo=None),
            },
        ]
        fake_db = FakeDB(docs)
        fake_bot = FakeBot()
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"db": fake_db}),
            bot=fake_bot,
        )

        await cleanup_expired_search_messages(context)

        self.assertEqual(fake_bot.deleted, [(-100123, 2), (-100123, 3)])
        self.assertEqual(fake_db.deleted_requests, ["result-not-yet-due"])
        self.assertEqual(fake_db.deleted_results, ["request-not-yet-due"])


if __name__ == "__main__":
    unittest.main()
