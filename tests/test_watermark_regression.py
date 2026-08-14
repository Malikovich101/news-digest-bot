import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import recent_memory_runtime


class WatermarkRegressionTests(unittest.TestCase):
    def test_failed_channel_does_not_advance_watermark(self):
        state = {
            "channels": {
                "@ok": {"last_checked_at": "2026-08-14T10:00:00+00:00", "last_message_id": 10},
                "@broken": {"last_checked_at": "2026-08-14T10:00:00+00:00", "last_message_id": 20},
            }
        }
        now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

        class Client:
            def iter_messages(self, channel, min_id=0):
                if channel == "@broken":
                    raise RuntimeError("network unavailable")
                return []

        original = recent_memory_runtime.digest._base_collect_posts

        def collect(client, channels, state, now, replay_hours=0):
            return original(client, channels, state, now, replay_hours=replay_hours)

        posts, next_state, failed = recent_memory_runtime.collect_posts(Client(), ["@ok", "@broken"], state, now)
        self.assertEqual(posts, [])
        self.assertEqual(failed, ["@broken"])
        self.assertEqual(next_state["channels"]["@ok"]["last_checked_at"], now.isoformat())
        self.assertEqual(next_state["channels"]["@broken"]["last_checked_at"], state["channels"]["@broken"]["last_checked_at"])
        self.assertEqual(next_state["channels"]["@broken"]["last_message_id"], 20)

    def test_previous_boundary_filters_overlap_for_each_channel(self):
        state = {
            "channels": {
                "@news": {"last_checked_at": "2026-08-14T15:01:00+00:00"}
            }
        }
        posts = [
            {"id": "@news:1", "channel": "@news", "date": "2026-08-14T12:07:00+00:00"},
            {"id": "@news:2", "channel": "@news", "date": "2026-08-14T15:01:00+00:00"},
            {"id": "@news:3", "channel": "@news", "date": "2026-08-14T15:02:00+00:00"},
        ]
        filtered, suppressed = recent_memory_runtime.filter_posts_after_last_check(posts, state)
        self.assertEqual([post["id"] for post in filtered], ["@news:3"])
        self.assertEqual(suppressed, 2)


if __name__ == "__main__":
    unittest.main()
