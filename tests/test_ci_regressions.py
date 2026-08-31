import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import digest


class CIRegressionTests(unittest.TestCase):
    def test_recent_news_prune_uses_current_72_hour_window(self):
        now = digest.utc_now()
        recent_news = [
            {"id": "@old:1", "date": (now - timedelta(hours=80)).isoformat(), "delivered_at": (now - timedelta(hours=80)).isoformat(), "text": "Old news"},
            {"id": "@recent:1", "date": (now - timedelta(hours=20)).isoformat(), "delivered_at": (now - timedelta(hours=20)).isoformat(), "text": "Recent news"},
        ]
        self.assertEqual([item["id"] for item in digest.prune_state(digest.migrate_state({"recent_news": recent_news}), now)["recent_news"]], ["@recent:1"])

    def test_partial_delivery_raises_after_retry_exhaustion(self):
        pipeline = digest.DigestPipeline()
        posts = [
            {"id": "@one:1", "channel": "@one", "message_id": 1, "date": "2026-08-23T12:00:00+00:00", "text": "first", "url": "https://t.me/one/1"},
            {"id": "@two:2", "channel": "@two", "message_id": 2, "date": "2026-08-23T12:01:00+00:00", "text": "second", "url": "https://t.me/two/2"},
        ]
        state = {"version": 8, "channels": {}, "pending_posts": {p["id"]: p for p in posts}, "delivered_ids": [], "delivery_receipts": {}, "recent_news": [], "completed_slots": {}}
        text = "first\n────────────\nsecond"
        class Response:
            def raise_for_status(self): return None
            def json(self): return {"ok": True}
        calls = []
        def send(url, data, timeout):
            calls.append(data["text"])
            if data["text"] == "second": raise digest.requests.ConnectionError("network unavailable")
            return Response()
        with patch("digest.requests.post", side_effect=send), patch("digest.time.sleep"), patch.object(pipeline, "save_state"):
            with self.assertRaises(RuntimeError): pipeline.send_telegram("token", "chat", text, state, posts)
        self.assertEqual(state["delivered_ids"], ["@one:1"])
        self.assertIn("@two:2", state["pending_posts"])

    def test_collect_posts_keeps_failed_channel_out_of_updates(self):
        pipeline = digest.DigestPipeline()
        now = digest.utc_now()
        state = {"channels": {"@broken": {"last_message_id": 20}}, "pending_posts": {}, "delivered_ids": []}
        class Client:
            def iter_messages(self, channel, min_id=0): raise RuntimeError("network unavailable")
        _, updates, failed = pipeline.collect_posts(Client(), ["@broken"], state, now)
        self.assertEqual(failed, ["@broken"])
        self.assertNotIn("@broken", updates)

    def test_gemini_failure_is_bounded_by_retry_count(self):
        class Models:
            def generate_content(self, model, contents, config): raise RuntimeError("503")
        with patch("digest.time.sleep"):
            with self.assertRaises(RuntimeError): digest.generate_json(SimpleNamespace(models=Models()), "test")


if __name__ == "__main__": unittest.main()
