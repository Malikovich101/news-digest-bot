import unittest

from recent_memory_runtime import filter_posts_after_last_check


class DigestOverlapRegressionTests(unittest.TestCase):
    def test_normal_run_suppresses_posts_at_or_before_previous_check(self):
        state = {
            "channels": {
                "@news": {
                    "last_checked_at": "2026-08-12T15:01:00+00:00",
                    "last_message_id": 100,
                }
            }
        }
        posts = [
            {"id": "@news:101", "channel": "@news", "date": "2026-08-12T12:07:00+00:00"},
            {"id": "@news:102", "channel": "@news", "date": "2026-08-12T15:01:00+00:00"},
            {"id": "@news:103", "channel": "@news", "date": "2026-08-12T15:02:00+00:00"},
        ]

        filtered, suppressed = filter_posts_after_last_check(posts, state, replay_hours=0)

        self.assertEqual([post["id"] for post in filtered], ["@news:103"])
        self.assertEqual(suppressed, 2)

    def test_replay_does_not_apply_normal_overlap_barrier(self):
        state = {
            "channels": {
                "@news": {
                    "last_checked_at": "2026-08-12T15:01:00+00:00",
                    "last_message_id": 100,
                }
            }
        }
        posts = [
            {"id": "@news:101", "channel": "@news", "date": "2026-08-12T12:07:00+00:00"},
        ]

        filtered, suppressed = filter_posts_after_last_check(posts, state, replay_hours=1)

        self.assertEqual(filtered, posts)
        self.assertEqual(suppressed, 0)
