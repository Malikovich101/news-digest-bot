import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import recent_memory_runtime as memory


class RecentMemoryTests(unittest.TestCase):
    def test_prune_keeps_all_items_within_36_hours(self):
        now = datetime(2026, 8, 11, 20, tzinfo=timezone.utc)
        history = [
            {
                "id": f"@channel:{index}",
                "date": (now - timedelta(hours=hours)).isoformat(),
                "delivered_at": (now - timedelta(hours=hours)).isoformat(),
                "text": f"Новость {index} про событие и технологию {index}",
            }
            for index, hours in enumerate([1] * 250)
        ]
        kept = memory.prune_recent_news(history, now)
        self.assertEqual(len(kept), 250)

    def test_prune_drops_only_items_older_than_36_hours(self):
        now = datetime(2026, 8, 11, 20, tzinfo=timezone.utc)
        recent = {
            "id": "@recent:1",
            "date": (now - timedelta(hours=35)).isoformat(),
            "delivered_at": (now - timedelta(hours=35)).isoformat(),
            "text": "Важная новость про Apple и новый iPhone",
        }
        old = {
            "id": "@old:1",
            "date": (now - timedelta(hours=37)).isoformat(),
            "delivered_at": (now - timedelta(hours=37)).isoformat(),
            "text": "Старая новость, которая должна быть удалена",
        }
        kept = memory.prune_recent_news([recent, old], now)
        self.assertEqual([item["id"] for item in kept], ["@recent:1"])

    def test_candidate_filter_finds_lexically_related_history(self):
        current = [{
            "id": "@current:1",
            "text": "Apple представила новый iPhone X на презентации",
        }]
        history = [
            {
                "id": "@history:1",
                "date": "2026-08-11T10:00:00+00:00",
                "delivered_at": "2026-08-11T10:00:00+00:00",
                "text": "Apple представила новый iPhone X на презентации",
            },
            {
                "id": "@history:2",
                "date": "2026-08-11T09:00:00+00:00",
                "delivered_at": "2026-08-11T09:00:00+00:00",
                "text": "Компания открыла новый логистический центр",
            },
        ]
        candidates = memory.recent_history_candidates(current, history, candidates_per_post=8)
        self.assertIn("@history:1", {item["id"] for item in candidates})

    def test_cross_run_memory_uses_only_candidate_history_for_gemini(self):
        current = [{
            "id": "@current:1",
            "text": "Apple представила новый iPhone X на презентации",
        }]
        history = [
            {
                "id": "@history:1",
                "date": "2026-08-11T10:00:00+00:00",
                "delivered_at": "2026-08-11T10:00:00+00:00",
                "text": "Apple представила новый iPhone X на презентации",
            },
            {
                "id": "@history:2",
                "date": "2026-08-11T09:00:00+00:00",
                "delivered_at": "2026-08-11T09:00:00+00:00",
                "text": "Компания открыла новый логистический центр",
            },
        ]

        prompts = []
        original_batch = memory.digest.make_ai_batches
        original_generate = memory.digest.generate_json
        try:
            memory.digest.make_ai_batches = lambda posts, text_limit, overlap=0: [posts]

            def fake_generate(client, prompt):
                prompts.append(prompt)
                return {"repeats": ["@current:1"]}

            memory.digest.generate_json = fake_generate
            kept, dropped = memory.cross_run_semantic_deduplicate(
                SimpleNamespace(), current, history
            )
        finally:
            memory.digest.make_ai_batches = original_batch
            memory.digest.generate_json = original_generate

        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)
        self.assertTrue(prompts)
        self.assertIn("@history:1", prompts[0])
        self.assertNotIn("@history:2", prompts[0])

    def test_digest_window_suppresses_messages_before_previous_check(self):
        state = {
            "channels": {
                "@media1337": {
                    "last_checked_at": "2026-08-11T10:00:00+00:00",
                    "last_message_id": 27754,
                }
            }
        }
        posts = [
            {
                "id": "@media1337:27752",
                "channel": "@media1337",
                "date": "2026-08-11T07:09:46+00:00",
                "text": "Старая публикация, которая уже попала в предыдущий период.",
            },
            {
                "id": "@media1337:27755",
                "channel": "@media1337",
                "date": "2026-08-11T10:01:00+00:00",
                "text": "Новая публикация после предыдущей проверки.",
            },
        ]
        kept, suppressed = memory.filter_posts_after_last_check(posts, state)
        self.assertEqual([item["id"] for item in kept], ["@media1337:27755"])
        self.assertEqual(suppressed, 1)

    def test_digest_window_does_not_apply_to_replay(self):
        state = {
            "channels": {
                "@media1337": {
                    "last_checked_at": "2026-08-11T10:00:00+00:00",
                    "last_message_id": 27754,
                }
            }
        }
        posts = [
            {
                "id": "@media1337:27752",
                "channel": "@media1337",
                "date": "2026-08-11T07:09:46+00:00",
                "text": "Старая публикация для ручного replay.",
            }
        ]
        kept, suppressed = memory.filter_posts_after_last_check(posts, state, replay_hours=1)
        self.assertEqual(kept, posts)
        self.assertEqual(suppressed, 0)

    def test_temporal_guard_preserves_max_gap_after_deduplication(self):
        start = datetime(2026, 8, 11, 16, tzinfo=timezone.utc)
        posts = []
        for index, hours in enumerate([0, 1, 2, 4, 4.5]):
            posts.append({
                "id": f"@current:{index}",
                "date": (start + timedelta(hours=hours)).isoformat(),
                "text": f"Новость {index}",
            })

        kept = memory._restore_temporal_coverage(posts, [posts[-1]])
        kept_dates = [datetime.fromisoformat(item["date"]) for item in kept]
        self.assertEqual(kept_dates, sorted(kept_dates))
        self.assertEqual(kept[0]["id"], "@current:0")
        self.assertEqual(kept[-1]["id"], "@current:4")
        self.assertTrue(all(
            right - left <= memory.MAX_SEMANTIC_COVERAGE_GAP
            for left, right in zip(kept_dates, kept_dates[1:])
        ))

    def test_temporal_guard_restores_all_posts_if_ai_drops_everything(self):
        posts = [
            {
                "id": "@current:1",
                "date": "2026-08-11T16:00:00+00:00",
                "text": "Новость 1",
            },
            {
                "id": "@current:2",
                "date": "2026-08-11T20:00:00+00:00",
                "text": "Новость 2",
            },
        ]

        original = memory.BASE_SEMANTIC_DEDUPLICATE
        try:
            memory.BASE_SEMANTIC_DEDUPLICATE = lambda client, current: ([], len(current))
            kept, dropped = memory.semantic_deduplicate_with_temporal_guard(
                SimpleNamespace(), posts
            )
        finally:
            memory.BASE_SEMANTIC_DEDUPLICATE = original

        self.assertEqual(kept, posts)
        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main()
