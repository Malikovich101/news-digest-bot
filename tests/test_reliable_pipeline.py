import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import requests

from digest_pipeline import DigestPipeline, format_digest, telegram_chunks


def post(source_id, text):
    channel, message_id = source_id.split(":")
    return {
        "id": source_id,
        "channel": channel,
        "message_id": int(message_id),
        "date": "2026-08-23T12:00:00+00:00",
        "text": text,
        "url": f"https://t.me/{channel.lstrip('@')}/{message_id}",
    }


class ReliablePipelineTests(unittest.TestCase):
    def test_legacy_state_migrates_without_losing_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                file.write('{"version":5,"channels":{"@news":{"last_message_id":42}},"recent_news":[{"id":"@old:1","date":"2026-08-22T10:00:00+00:00","delivered_at":"2026-08-22T10:01:00+00:00","text":"Old event"}]}')
            state = DigestPipeline(state_file=path).load_state()
            self.assertEqual(state["version"], 6)
            self.assertEqual(state["channels"]["@news"]["last_message_id"], 42)
            self.assertEqual(state["event_memory"][0]["id"], "@old:1")

    def test_pending_post_survives_processing_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "state.json")
            pipeline = DigestPipeline(state_file=path)
            state = pipeline.load_state()
            pipeline.add_pending_posts(state, [post("@news:100", "Новая публикация с достаточным количеством текста.")], datetime.now(timezone.utc))
            reloaded = pipeline.load_state()
            self.assertIn("@news:100", reloaded["pending_posts"])

    def test_pending_posts_are_retried_before_new_collection(self):
        pipeline = DigestPipeline()
        state = {"channels": {}, "pending_posts": {"@news:100": post("@news:100", "Незавершённая публикация")}, "delivered_ids": []}

        class Client:
            def iter_messages(self, *args, **kwargs):
                raise AssertionError("Telegram must not be queried while pending posts exist")

        collected, updates, failed = pipeline.collect_posts(Client(), ["@news"], state, datetime.now(timezone.utc))
        self.assertEqual(collected, [])
        self.assertEqual(updates, {})
        self.assertEqual(failed, [])

    def test_format_digest_preserves_long_original_text(self):
        original = "A" * 7000
        rendered = format_digest(
            [post("@news:1", original)],
            {"source_posts": 1, "short": 0, "ads": 0, "ad_review": 0, "python_duplicates": 0},
            semantic_duplicates=0,
        )
        self.assertIn(original, rendered)

    def test_telegram_chunks_preserve_all_text(self):
        original = ("0123456789" * 1000) + "\n" + ("abcdefghij" * 1000)
        chunks = list(telegram_chunks(original))
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), original.replace("\n", ""))

    def test_delivery_receipt_keeps_post_ids_without_parsing_chunk_text(self):
        pipeline = DigestPipeline(state_file="/tmp/news-digest-receipt-test.json")
        posts = [post("@news:1", "Original text")]
        state = {"version": 6, "channels": {}, "pending_posts": {"@news:1": posts[0]}, "delivered_ids": [], "delivery_receipts": {}, "delivered_chunks": [], "recent_news": [], "event_memory": []}
        record = {"id": "receipt-1", "text": "rendered presentation", "post_ids": ["@news:1"]}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        with patch("digest_pipeline.requests.post", return_value=Response()), patch("digest_pipeline.time.sleep"), patch.object(pipeline, "save_state"):
            pipeline.send_telegram("token", "chat", record["text"], state, posts, rendered_chunks=[record])

        self.assertEqual(state["delivered_ids"], ["@news:1"])
        self.assertNotIn("@news:1", state["pending_posts"])
        self.assertEqual(state["delivery_receipts"]["receipt-1"]["post_ids"], ["@news:1"])

    def test_partial_delivery_checkpoint_is_idempotent(self):
        pipeline = DigestPipeline(state_file="/tmp/news-digest-partial-test.json")
        posts = [post("@one:1", "A" * 100), post("@two:2", "B" * 100)]
        state = {"version": 6, "channels": {}, "pending_posts": {p["id"]: p for p in posts}, "delivered_ids": [], "delivery_receipts": {}, "delivered_chunks": [], "recent_news": [], "event_memory": []}
        chunks = [
            {"id": "chunk-1", "text": "first", "post_ids": ["@one:1"]},
            {"id": "chunk-2", "text": "second", "post_ids": ["@two:2"]},
        ]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        calls = []
        def send(url, data, timeout):
            calls.append(data["text"])
            if len(calls) == 2:
                raise requests.ConnectionError("network unavailable")
            return Response()

        with patch("digest_pipeline.requests.post", side_effect=send), patch("digest_pipeline.time.sleep"), patch.object(pipeline, "save_state"):
            with self.assertRaises(RuntimeError):
                pipeline.send_telegram("token", "chat", "ignored", state, posts, rendered_chunks=chunks)

        self.assertEqual(state["delivered_ids"], ["@one:1"])
        self.assertIn("@two:2", state["pending_posts"])
        self.assertIn("chunk-1", state["delivery_receipts"])


if __name__ == "__main__":
    unittest.main()
