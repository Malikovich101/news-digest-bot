import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from digest_pipeline import DigestPipeline


def post(text, source_id):
    channel, message_id = source_id.split(":")
    return {
        "id": source_id,
        "channel": channel,
        "message_id": int(message_id),
        "date": "2026-08-13T00:00:00+00:00",
        "text": text,
        "url": f"https://t.me/{channel.lstrip('@')}/{message_id}",
    }


class DeliveryIdempotencyTests(unittest.TestCase):
    def test_successfully_delivered_posts_are_checkpointed_before_later_failure(self):
        first = "A" * 3500
        second = "B" * 3500
        posts = [post(first, "@one:1"), post(second, "@two:2")]
        text = (
            "header\n────────────\n"
            + first
            + "\nИсточник: https://t.me/one/1\n────────────\n"
            + second
            + "\nИсточник: https://t.me/two/2"
        )
        state = {"version": 5, "channels": {}, "delivered_ids": [], "delivered_chunks": []}
        pipeline = DigestPipeline(state_file="/tmp/news-digest-delivery-test.json")

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        calls = []

        def send(url, data, timeout):
            calls.append(data["text"])
            if len(calls) >= 2:
                raise requests.ConnectionError("network unavailable")
            return Response()

        with patch.object(pipeline, "save_state"), \
             patch("digest_pipeline.requests.post", side_effect=send), \
             patch("digest_pipeline.time.sleep"):
            with self.assertRaises(RuntimeError):
                pipeline.send_telegram("token", "chat", text, state=state, posts=posts)

        self.assertEqual(state["delivered_ids"], ["@one:1"])


if __name__ == "__main__":
    unittest.main()
