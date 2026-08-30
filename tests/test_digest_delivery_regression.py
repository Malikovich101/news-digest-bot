import os
import unittest
from unittest.mock import patch
import requests
from digest import DigestPipeline


def post(text, source_id):
    channel, message_id = source_id.split(":")
    return {"id": source_id, "channel": channel, "message_id": int(message_id), "date": "2026-08-13T00:00:00+00:00", "text": text, "url": f"https://t.me/{channel.lstrip('@')}/{message_id}"}


class DeliveryRegressionTests(unittest.TestCase):
    def test_channel_watermark_waits_for_successful_telegram_delivery(self):
        old_watermark = {"last_message_id": 10, "last_checked_at": "2026-08-30T00:00:00+00:00"}
        new_watermark = {"last_message_id": 11, "last_checked_at": "2026-08-30T01:00:00+00:00"}
        item = post("Новость, которую нельзя потерять", "@one:11")
        state = {
            "version": 7,
            "channels": {"@one": old_watermark.copy()},
            "pending_posts": {},
            "delivered_ids": [],
            "delivery_receipts": {},
            "delivered_chunks": [],
            "recent_news": [],
            "event_memory": [],
            "completed_slots": {},
        }
        stats = {"source_posts": 1, "ads": 0, "short": 0, "python_duplicates": 0, "ad_review": 0}
        pipeline = DigestPipeline(state_file="/tmp/news-digest-watermark-regression.json")
        environment = {
            "TG_API_ID": "1",
            "TG_API_HASH": "hash",
            "TG_SESSION_STRING": "session",
            "TG_BOT_TOKEN": "token",
            "TG_CHAT_ID": "chat",
            "REPLAY_HOURS": "0",
        }
        with patch.dict(os.environ, environment, clear=False), \
             patch.object(pipeline, "load_state", return_value=state), \
             patch.object(pipeline, "save_state"), \
             patch.object(pipeline, "collect_posts", return_value=([item], {"@one": new_watermark}, [])), \
             patch("digest.filter_and_deduplicate", return_value=([item], stats)), \
             patch.object(pipeline, "send_telegram", side_effect=RuntimeError("Telegram unavailable")), \
             patch("digest.StringSession"), \
             patch("digest.TelegramClient"):
            with self.assertRaisesRegex(RuntimeError, "Telegram unavailable"):
                pipeline.run()
        self.assertEqual(state["channels"]["@one"], old_watermark)

    def test_empty_digest_is_sent_for_each_slot(self):
        state = {"version": 7, "channels": {}, "pending_posts": {}, "delivered_ids": [], "delivery_receipts": {"same": {"sent_at": "2026-08-30T00:00:00+00:00", "post_ids": []}}, "delivered_chunks": ["same"], "recent_news": [], "event_memory": [], "completed_slots": {}}
        chunk = {"id": "same", "text": "За этот период новых подходящих новостей не было.", "post_ids": []}
        class Response:
            def raise_for_status(self): return None
            def json(self): return {"ok": True}
        pipeline = DigestPipeline(state_file="/tmp/news-digest-empty-regression.json")
        with patch.object(pipeline, "save_state"), patch("digest.requests.post", return_value=Response()) as send, patch("digest.time.sleep"):
            pipeline.send_telegram("token", "chat", "", state, [], rendered_chunks=[chunk], allow_repeat=True)
        self.assertEqual(send.call_count, 1)

    def test_successful_chunk_is_checkpointed_before_later_failure(self):
        posts = [post("A" * 100, "@one:1"), post("B" * 100, "@two:2")]
        state = {"version": 6, "channels": {}, "pending_posts": {p["id"]: p for p in posts}, "delivered_ids": [], "delivery_receipts": {}, "delivered_chunks": [], "recent_news": [], "event_memory": []}
        chunks = [{"id": "chunk-1", "text": "first", "post_ids": ["@one:1"]}, {"id": "chunk-2", "text": "second", "post_ids": ["@two:2"]}]
        class Response:
            def raise_for_status(self): return None
            def json(self): return {"ok": True}
        calls=[]
        def send(url, data, timeout):
            calls.append(data["text"])
            if data["text"] == "second":
                raise requests.ConnectionError("network unavailable")
            return Response()
        pipeline = DigestPipeline(state_file="/tmp/news-digest-delivery-regression.json")
        with patch.object(pipeline, "save_state"), patch("digest.requests.post", side_effect=send), patch("digest.time.sleep"):
            with self.assertRaises(RuntimeError): pipeline.send_telegram("token", "chat", "", state, posts, rendered_chunks=chunks)
        self.assertEqual(state["delivered_ids"], ["@one:1"])
        self.assertIn("@two:2", state["pending_posts"])
        self.assertIn("chunk-1", state["delivery_receipts"])


if __name__ == "__main__": unittest.main()
