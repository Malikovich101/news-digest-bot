import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import digest
import recent_memory_runtime


class RecentMemoryTests(unittest.TestCase):
    def test_prune_drops_only_items_older_than_36_hours(self):
        now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        history = [
            {"id": "old", "date": "2026-08-10T11:00:00+00:00", "delivered_at": "2026-08-10T11:00:00+00:00", "text": "old"},
            {"id": "new", "date": "2026-08-11T00:00:00+00:00", "delivered_at": "2026-08-11T00:00:00+00:00", "text": "new"},
        ]
        pruned = recent_memory_runtime.prune_recent_news(history, now)
        self.assertEqual([item["id"] for item in pruned], ["new"])

    def test_prune_keeps_all_items_within_36_hours(self):
        now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        history = [
            {"id": "a", "date": "2026-08-11T00:00:00+00:00", "delivered_at": "2026-08-11T00:00:00+00:00", "text": "a"},
            {"id": "b", "date": "2026-08-11T23:00:00+00:00", "delivered_at": "2026-08-11T23:00:00+00:00", "text": "b"},
        ]
        pruned = recent_memory_runtime.prune_recent_news(history, now)
        self.assertEqual({item["id"] for item in pruned}, {"a", "b"})

    def test_candidate_filter_finds_lexically_related_history(self):
        posts = [{"id": "current", "text": "Apple представила новый iPhone X на презентации."}]
        history = [{"id": "history", "text": "Apple представила новый iPhone X.", "delivered_at": "2026-08-12T10:00:00+00:00"}]
        candidates = recent_memory_runtime.recent_history_candidates(posts, history)
        self.assertEqual([item["id"] for item in candidates], ["history"])

    def test_cross_run_memory_uses_only_candidate_history_for_gemini(self):
        current_posts = [{"id": "current", "text": "Apple представила новый iPhone X на презентации."}]
        history = [
            {"id": "related", "text": "Apple представила новый iPhone X.", "date": "2026-08-12T10:00:00+00:00", "delivered_at": "2026-08-12T10:00:00+00:00"},
            {"id": "unrelated", "text": "Новости о погоде в регионе.", "date": "2026-08-12T09:00:00+00:00", "delivered_at": "2026-08-12T09:00:00+00:00"},
        ]

        seen = []

        def fake_generate_json(client, prompt):
            seen.append(prompt)
            return {"repeats": ["current"]}

        with patch.object(digest, "generate_json", side_effect=fake_generate_json):
            kept, dropped = recent_memory_runtime.cross_run_semantic_deduplicate(SimpleNamespace(), current_posts, history)

        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)
        self.assertEqual(len(seen), 1)
        self.assertIn("related", seen[0])
        self.assertNotIn("unrelated", seen[0])

    def test_digest_window_suppresses_messages_before_previous_check(self):
        state = {"channels": {"@news": {"last_checked_at": "2026-08-12T15:01:00+00:00"}}}
        posts = [
            {"id": "@news:1", "channel": "@news", "date": "2026-08-12T12:07:00+00:00"},
            {"id": "@news:2", "channel": "@news", "date": "2026-08-12T15:01:00+00:00"},
            {"id": "@news:3", "channel": "@news", "date": "2026-08-12T15:02:00+00:00"},
        ]
        filtered, suppressed = recent_memory_runtime.filter_posts_after_last_check(posts, state)
        self.assertEqual([item["id"] for item in filtered], ["@news:3"])
        self.assertEqual(suppressed, 2)

    def test_digest_window_does_not_apply_to_replay(self):
        state = {"channels": {"@news": {"last_checked_at": "2026-08-12T15:01:00+00:00"}}}
        posts = [{"id": "@news:1", "channel": "@news", "date": "2026-08-12T12:07:00+00:00"}]
        filtered, suppressed = recent_memory_runtime.filter_posts_after_last_check(posts, state, replay_hours=1)
        self.assertEqual(filtered, posts)
        self.assertEqual(suppressed, 0)

    def test_temporal_guard_was_removed(self):
        self.assertFalse(hasattr(recent_memory_runtime, "_restore_temporal_coverage"))
        self.assertFalse(hasattr(recent_memory_runtime, "MAX_SEMANTIC_COVERAGE_GAP"))

    def test_state_recent_news_has_no_artificial_count_cap(self):
        now = datetime.now(timezone.utc)
        history = [
            {
                "id": str(index),
                "date": now.isoformat(),
                "delivered_at": (now - timedelta(minutes=index)).isoformat(),
                "text": f"news {index}",
            }
            for index in range(250)
        ]
        pruned = recent_memory_runtime.prune_recent_news(history, now)
        self.assertEqual(len(pruned), 250)


if __name__ == "__main__":
    unittest.main()
