import unittest
from types import SimpleNamespace
from unittest.mock import patch

import digest_pipeline


class DigestPipelineTests(unittest.TestCase):
    def test_telegram_chunks_are_rate_limited(self):
        pipeline = digest_pipeline.DigestPipeline(state_file="/tmp/news-digest-test-state.json")
        state = {"version": 5, "delivered_chunks": [], "delivered_ids": [], "recent_news": [], "channels": {}}
        posts = [
            {"id": "@one:1", "url": "https://t.me/one/1", "text": "A" * 100},
            {"id": "@two:2", "url": "https://t.me/two/2", "text": "B" * 100},
        ]
        text = "\n────────────\n".join([
            "A" * 2000 + "\nИсточник: https://t.me/one/1",
            "B" * 2000 + "\nИсточник: https://t.me/two/2",
        ])

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        with patch("digest_pipeline.requests.post", return_value=Response()), \
             patch("digest_pipeline.time.sleep") as sleep, \
             patch.object(pipeline, "save_state"):
            pipeline.send_telegram("token", "chat", text, state, posts)

        self.assertGreaterEqual(sleep.call_count, 2)
        sleep.assert_any_call(digest_pipeline.CHUNK_DELAY_SECONDS)

    def test_collection_updates_are_pending_until_after_delivery(self):
        pipeline = digest_pipeline.DigestPipeline()
        state = {
            "channels": {
                "@news": {"last_checked_at": "2026-08-14T10:00:00+00:00", "last_message_id": 10}
            },
            "delivered_ids": [],
        }
        client = SimpleNamespace(iter_messages=lambda channel, min_id=0: [])
        posts, updates, failed = pipeline.collect_posts(
            client,
            ["@news"],
            state,
            pipeline.parse_datetime("2026-08-14T12:00:00+00:00"),
        )
        self.assertEqual(posts, [])
        self.assertEqual(failed, [])
        self.assertEqual(state["channels"]["@news"]["last_checked_at"], "2026-08-14T10:00:00+00:00")
        self.assertEqual(updates["@news"]["last_checked_at"], "2026-08-14T12:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
