import os
import unittest
from datetime import timedelta
from unittest.mock import patch

import digest


class CurrentPipelineRegressionTests(unittest.TestCase):
    def test_delivery_receipts_older_than_three_days_are_pruned(self):
        now = digest.utc_now()
        state = digest.migrate_state({"delivery_receipts": {
            "old": {"sent_at": (now - timedelta(hours=80)).isoformat(), "post_ids": []},
            "recent": {"sent_at": (now - timedelta(hours=2)).isoformat(), "post_ids": []},
        }})
        result = digest.prune_state(state, now)
        self.assertEqual(set(result["delivery_receipts"]), {"recent"})

    def test_recent_news_older_than_72_hours_is_pruned(self):
        now = digest.utc_now()
        items = [
            {"id":"old","date":(now-timedelta(hours=80)).isoformat(),"delivered_at":(now-timedelta(hours=80)).isoformat(),"text":"Old"},
            {"id":"recent","date":(now-timedelta(hours=10)).isoformat(),"delivered_at":(now-timedelta(hours=10)).isoformat(),"text":"Recent"},
        ]
        result = digest.prune_state(digest.migrate_state({"recent_news": items}), now)["recent_news"]
        self.assertEqual([item["id"] for item in result], ["recent"])

    def test_legacy_state_migrates_to_v8(self):
        raw={"version":5,"channels":{"@news":{"last_message_id":10}},"last_successful_run":"2026-08-22T09:20:00+00:00","recent_news":[{"id":"@news:1","date":"2026-08-22T10:00:00+00:00","delivered_at":"2026-08-22T10:01:00+00:00","text":"Old"}]}
        state=digest.migrate_state(raw)
        self.assertEqual(state["version"],8)
        self.assertEqual(state["recent_news"][0]["id"],"@news:1")

    def test_pending_posts_are_kept_for_retry(self):
        state=digest.migrate_state({"pending_posts":{"@news:1":{"id":"@news:1","collected_at":digest.utc_now().isoformat(),"text":"Retry me","url":"https://t.me/news/1"}}})
        self.assertIn("@news:1", state["pending_posts"])

    def test_pending_posts_are_retried_before_fresh_posts(self):
        pipeline = digest.DigestPipeline()
        pending = {
            "@news:1": {
                "id": "@news:1", "channel": "@news", "message_id": 1,
                "date": "2026-09-01T10:00:00+00:00", "text": "Retry me",
                "url": "https://t.me/news/1", "collected_at": "2026-09-01T10:01:00+00:00",
            }
        }
        state = {"version": 8, "channels": {}, "pending_posts": pending, "delivered_ids": [], "delivery_receipts": {}, "recent_news": [], "completed_slots": {}}
        sent = []

        def fake_send(token, chat_id, text, current_state, posts):
            sent.extend(post["id"] for post in posts)
            for post in posts:
                current_state["pending_posts"].pop(post["id"], None)

        with patch("digest.require_environment"), \
             patch("digest.load_channels", return_value=["@news"]), \
             patch("digest.TelegramClient"), \
             patch.object(pipeline, "load_state", return_value=state), \
             patch.object(pipeline, "collect_posts", return_value=([], {"@news": {"last_message_id": 1, "last_checked_at": "2026-09-01T12:00:00+00:00"}}, [])), \
             patch.object(pipeline, "send_telegram", side_effect=fake_send), \
             patch.object(pipeline, "save_state"):
            with patch.dict(os.environ, {
                "TG_API_ID":"1",
                "TG_API_HASH":"h",
                "TG_SESSION_STRING":"valid-test-placeholder",
                "TG_BOT_TOKEN":"t",
                "TG_CHAT_ID":"c",
            }, clear=True):
                result = pipeline.run()

        self.assertEqual(result["status"], "success")
        self.assertEqual(sent, ["@news:1"])
        self.assertNotIn("@news:1", state["pending_posts"])

    def test_digest_output_is_chronological(self):
        posts = [
            {"id":"@news:2","channel":"@news","message_id":2,"date":"2026-08-31T12:00:00+00:00","text":"Newer","url":"https://t.me/news/2"},
            {"id":"@news:1","channel":"@news","message_id":1,"date":"2026-08-31T10:00:00+00:00","text":"Older","url":"https://t.me/news/1"},
        ]
        posts.sort(key=lambda post: digest.parse_datetime(post["date"]))
        self.assertEqual([post["id"] for post in posts], ["@news:1", "@news:2"])


if __name__ == "__main__":
    unittest.main()
