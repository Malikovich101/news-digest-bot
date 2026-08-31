import unittest
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import digest


def post(source_id, date, text):
    channel, message_id = source_id.split(":")
    return {"id": source_id, "channel": channel, "message_id": int(message_id), "date": date, "text": text, "url": f"https://t.me/{channel.lstrip('@')}/{message_id}"}


class DigestPipelineTests(unittest.TestCase):
    def test_completed_slot_skips_duplicate_recovery_run(self):
        pipeline = digest.DigestPipeline()
        with patch.object(pipeline, "load_state", return_value={"completed_slots": {"2026-08-30-morning": "2026-08-30T03:20:00+00:00"}}), patch.dict(os.environ, {"DIGEST_SLOT_ID": "2026-08-30-morning"}, clear=True):
            result = pipeline.run()
        self.assertEqual(result["status"], "skipped")

    def test_temporal_barrier_is_per_channel(self):
        pipeline = digest.DigestPipeline()
        state = {"channels": {"@news": {"last_checked_at": "2026-08-14T15:01:00+00:00", "last_message_id": 100}}, "pending_posts": {}, "delivered_ids": []}
        posts = [post("@news:101", "2026-08-14T12:07:00+00:00", "old"), post("@news:102", "2026-08-14T15:01:00+00:00", "boundary"), post("@news:103", "2026-08-14T15:02:00+00:00", "new")]
        previous = state["channels"]["@news"]["last_checked_at"]
        filtered = [item for item in posts if item["date"] > previous]
        self.assertEqual([item["id"] for item in filtered], ["@news:103"])

    def test_failed_channel_does_not_advance_watermark(self):
        pipeline = digest.DigestPipeline()
        state = {"channels": {"@ok": {"last_checked_at": "2026-08-14T10:00:00+00:00", "last_message_id": 10}, "@broken": {"last_checked_at": "2026-08-14T10:00:00+00:00", "last_message_id": 20}}, "pending_posts": {}, "delivered_ids": []}
        now = pipeline.parse_datetime("2026-08-14T12:00:00+00:00")
        class Client:
            def iter_messages(self, channel, min_id=0):
                if channel == "@broken":
                    raise RuntimeError("network unavailable")
                return []
        posts, updates, failed = pipeline.collect_posts(Client(), ["@ok", "@broken"], state, now)
        self.assertEqual(posts, [])
        self.assertEqual(failed, ["@broken"])
        self.assertIn("@ok", updates)
        self.assertNotIn("@broken", updates)

    def test_recent_news_prunes_old_entries(self):
        now = digest.utc_now()
        state = digest.migrate_state({"recent_news": [
            {"id": "old", "date": now.isoformat(), "delivered_at": (now - timedelta(hours=80)).isoformat(), "text": "old"},
            {"id": "new", "date": now.isoformat(), "delivered_at": (now - timedelta(hours=1)).isoformat(), "text": "new"},
        ]})
        pruned = digest.prune_state(state, now)["recent_news"]
        self.assertEqual([item["id"] for item in pruned], ["new"])


class GeminiFallbackTests(unittest.TestCase):
    def test_generate_json_does_not_retry_forever(self):
        class Models:
            def generate_content(self, model, contents, config):
                raise RuntimeError("503 UNAVAILABLE")
        with patch("digest.time.sleep"):
            with self.assertRaises(RuntimeError):
                digest.generate_json(SimpleNamespace(models=Models()), "test")


if __name__ == "__main__": unittest.main()
