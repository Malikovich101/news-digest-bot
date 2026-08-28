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
