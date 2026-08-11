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
                "delivered_at": "2026-08-11T10:00:00+00:00",
                "text": "Apple представила новый iPhone X на презентации",
            },
            {
                "id": "@history:2",
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
                "delivered_at": "2026-08-11T10:00:00+00:00",
                "text": "Apple представила новый iPhone X на презентации",
            },
            {
                "id": "@history:2",
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


if __name__ == "__main__":
    unittest.main()
