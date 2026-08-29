import unittest
from datetime import timedelta

import digest


class CurrentPipelineRegressionTests(unittest.TestCase):
    def test_recent_news_older_than_72_hours_is_pruned(self):
        now = digest.utc_now()
        items = [
            {"id":"old","date":(now-timedelta(hours=80)).isoformat(),"delivered_at":(now-timedelta(hours=80)).isoformat(),"text":"Old"},
            {"id":"recent","date":(now-timedelta(hours=10)).isoformat(),"delivered_at":(now-timedelta(hours=10)).isoformat(),"text":"Recent"},
        ]
        result = digest.prune_recent_news(items, now)
        self.assertEqual([item["id"] for item in result], ["recent"])

    def test_legacy_state_migrates_to_v7(self):
        raw={"version":5,"channels":{"@news":{"last_message_id":10}},"recent_news":[{"id":"@news:1","date":"2026-08-22T10:00:00+00:00","delivered_at":"2026-08-22T10:01:00+00:00","text":"Old"}]}
        state=digest.migrate_state(raw)
        self.assertEqual(state["version"],7)
        self.assertEqual(state["event_memory"][0]["id"],"@news:1")
        self.assertEqual(state["completed_slots"], {})

    def test_pending_posts_are_kept_for_retry(self):
        state=digest.migrate_state({"pending_posts":{"@news:1":{"id":"@news:1","collected_at":digest.utc_now().isoformat()}}})
        self.assertIn("@news:1", state["pending_posts"])


if __name__ == "__main__":
    unittest.main()
