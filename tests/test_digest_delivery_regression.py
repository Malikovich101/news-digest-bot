import unittest
from unittest.mock import patch
import requests
from digest import DigestPipeline


def post(text, source_id):
    channel, message_id = source_id.split(":")
    return {"id": source_id, "channel": channel, "message_id": int(message_id), "date": "2026-08-13T00:00:00+00:00", "text": text, "url": f"https://t.me/{channel.lstrip('@')}/{message_id}"}


class DeliveryRegressionTests(unittest.TestCase):
    def test_successful_chunk_is_checkpointed_before_later_failure(self):
        posts = [post("A" * 100, "@one:1"), post("B" * 100, "@two:2")]
        state = {"version": 8, "channels": {}, "pending_posts": {p["id"]: p for p in posts}, "delivered_ids": [], "delivery_receipts": {}, "recent_news": [], "completed_slots": {}}
        chunks = [{"id": "chunk-1", "text": "first", "post_ids": ["@one:1"]}, {"id": "chunk-2", "text": "second", "post_ids": ["@two:2"]}]
        class Response:
            def raise_for_status(self): return None
            def json(self): return {"ok": True}
        def send(url, data, timeout):
            if data["text"] == "second":
                raise requests.ConnectionError("network unavailable")
            return Response()
        pipeline = DigestPipeline()
        with patch.object(pipeline, "save_state"), patch("digest.requests.post", side_effect=send), patch("digest.time.sleep"):
            with self.assertRaises(RuntimeError): pipeline.send_telegram("token", "chat", "", state, posts, rendered_chunks=chunks)
        self.assertEqual(state["delivered_ids"], ["@one:1"])
        self.assertIn("@two:2", state["pending_posts"])
        self.assertIn("chunk-1", state["delivery_receipts"])

    def test_failed_collection_never_advances_watermark(self):
        pipeline = DigestPipeline()
        state = {"channels": {"@broken": {"last_message_id": 20}}, "pending_posts": {}, "delivered_ids": []}
        class Client:
            def iter_messages(self, channel, min_id=0):
                raise RuntimeError("network unavailable")
        _, updates, failed = pipeline.collect_posts(Client(), ["@broken"], state, pipeline.parse_datetime("2026-08-30T12:00:00+00:00"))
        self.assertEqual(failed, ["@broken"])
        self.assertNotIn("@broken", updates)


if __name__ == "__main__": unittest.main()
