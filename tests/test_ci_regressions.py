import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import digest


class CIRegressionTests(unittest.TestCase):
    def test_recent_news_prune_uses_current_72_hour_window(self):
        now = digest.utc_now()
        recent_news = [
            {
                "id": "@old:1",
                "date": (now - timedelta(hours=80)).isoformat(),
                "delivered_at": (now - timedelta(hours=80)).isoformat(),
                "text": "Old news",
            },
            {
                "id": "@recent:1",
                "date": (now - timedelta(hours=20)).isoformat(),
                "delivered_at": (now - timedelta(hours=20)).isoformat(),
                "text": "Recent news",
            },
        ]
        pruned = digest.prune_recent_news(recent_news, now)
        self.assertEqual([item["id"] for item in pruned], ["@recent:1"])

    def test_partial_delivery_raises_after_retry_exhaustion(self):
        pipeline = digest.DigestPipeline(state_file="/tmp/news-digest-ci-regression.json")
        posts = [
            {
                "id": "@one:1",
                "channel": "@one",
                "message_id": 1,
                "date": "2026-08-23T12:00:00+00:00",
                "text": "first",
                "url": "https://t.me/one/1",
            },
            {
                "id": "@two:2",
                "channel": "@two",
                "message_id": 2,
                "date": "2026-08-23T12:01:00+00:00",
                "text": "second",
                "url": "https://t.me/two/2",
            },
        ]
        state = {
            "version": 6,
            "channels": {},
            "pending_posts": {p["id"]: p for p in posts},
            "delivered_ids": [],
            "delivery_receipts": {},
            "delivered_chunks": [],
            "recent_news": [],
            "event_memory": [],
        }
        chunks = [
            {"id": "chunk-1", "text": "first chunk", "post_ids": ["@one:1"]},
            {"id": "chunk-2", "text": "second chunk", "post_ids": ["@two:2"]},
        ]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        calls = []

        def send(url, data, timeout):
            calls.append(data["text"])
            if data["text"] == "second chunk":
                raise digest.requests.ConnectionError("network unavailable")
            return Response()

        with patch("digest.requests.post", side_effect=send), patch("digest.time.sleep"), patch.object(pipeline, "save_state"):
            with self.assertRaises(RuntimeError):
                pipeline.send_telegram("token", "chat", "", state, posts, rendered_chunks=chunks)

        self.assertEqual(state["delivered_ids"], ["@one:1"])
        self.assertIn("@two:2", state["pending_posts"])

    def test_pending_collection_does_not_mark_channel_as_failed(self):
        pipeline = digest.DigestPipeline()
        now = digest.utc_now()
        state = {
            "channels": {},
            "pending_posts": {
                "@news:100": {
                    "id": "@news:100",
                    "channel": "@news",
                    "message_id": 100,
                    "date": now.isoformat(),
                    "text": "pending",
                    "url": "https://t.me/news/100",
                }
            },
            "delivered_ids": [],
        }

        # Runtime handles pending posts before normal collection; this test
        # documents that the collection layer itself still has no fake failure state.
        class Client:
            def iter_messages(self, *args, **kwargs):
                return []

        _, updates, failed = pipeline.collect_posts(Client(), ["@news"], state, now)
        self.assertNotIn("@news", failed)
        self.assertIn("@news", updates)


if __name__ == "__main__":
    unittest.main()
