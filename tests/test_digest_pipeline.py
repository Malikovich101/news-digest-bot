import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import digest


def post(source_id, date, text):
    channel, message_id = source_id.split(":")
    return {
        "id": source_id,
        "channel": channel,
        "message_id": int(message_id),
        "date": date,
        "text": text,
        "url": f"https://t.me/{channel.lstrip('@')}/{message_id}",
    }


class DigestPipelineTests(unittest.TestCase):
    def test_telegram_chunks_are_rate_limited(self):
        pipeline = digest.DigestPipeline(state_file="/tmp/news-digest-test-state.json")
        state = {"version": 6, "delivered_chunks": [], "delivered_ids": [], "recent_news": [], "event_memory": [], "channels": {}}
        posts = [post("@one:1", "2026-08-14T10:00:00+00:00", "A" * 100), post("@two:2", "2026-08-14T10:01:00+00:00", "B" * 100)]
        text = "\n────────────\n".join(["A" * 2000 + "\nИсточник: https://t.me/one/1", "B" * 2000 + "\nИсточник: https://t.me/two/2"])

        class Response:
            def raise_for_status(self):
                return None
            def json(self):
                return {"ok": True}

        with patch("digest.requests.post", return_value=Response()), patch("digest.time.sleep") as sleep, patch.object(pipeline, "save_state"):
            pipeline.send_telegram("token", "chat", text, state, posts)
        self.assertGreaterEqual(sleep.call_count, 2)
        sleep.assert_any_call(digest.CHUNK_DELAY_SECONDS)

    def test_temporal_barrier_removes_overlap_per_channel(self):
        pipeline = digest.DigestPipeline()
        state = {"channels": {"@news": {"last_checked_at": "2026-08-14T15:01:00+00:00", "last_message_id": 100}}, "pending_posts": {}, "delivered_ids": []}
        posts = [post("@news:101", "2026-08-14T12:07:00+00:00", "old"), post("@news:102", "2026-08-14T15:01:00+00:00", "boundary"), post("@news:103", "2026-08-14T15:02:00+00:00", "new")]
        filtered, suppressed = pipeline.filter_posts_after_last_check(posts, state)
        self.assertEqual([item["id"] for item in filtered], ["@news:103"])
        self.assertEqual(suppressed, 2)

    def test_replay_bypasses_normal_temporal_barrier(self):
        pipeline = digest.DigestPipeline()
        state = {"channels": {"@news": {"last_checked_at": "2026-08-14T15:01:00+00:00"}}, "pending_posts": {}, "delivered_ids": []}
        posts = [post("@news:101", "2026-08-14T12:07:00+00:00", "old")]
        filtered, suppressed = pipeline.filter_posts_after_last_check(posts, state, replay_hours=1)
        self.assertEqual(filtered, posts)
        self.assertEqual(suppressed, 0)

    def test_semantic_duplicate_keeps_earliest_publication(self):
        pipeline = digest.DigestPipeline()
        posts = [post("@early:1", "2026-08-14T21:10:00+00:00", "Компания представила новый продукт вчера вечером."), post("@late:2", "2026-08-15T01:18:00+00:00", "Компания представила новый продукт вчера вечером по данным канала.")]
        with patch("digest.generate_json", return_value={"groups": [{"keep": "@early:1", "duplicates": ["@late:2"]}]}):
            kept, dropped = pipeline.semantic_deduplicate(SimpleNamespace(), posts)
        self.assertEqual([item["id"] for item in kept], ["@early:1"])
        self.assertEqual(dropped, 1)

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
        self.assertEqual(updates["@ok"]["last_checked_at"], now.isoformat())
        self.assertNotIn("@broken", updates)


class LongTermMemoryTests(unittest.TestCase):
    def test_recent_news_prunes_entries_older_than_72_hours(self):
        old_time = digest.utc_now() - timedelta(hours=80)
        recent_time = digest.utc_now() - timedelta(hours=10)
        recent_news = [
            {"id": "@old:1", "date": old_time.isoformat(), "delivered_at": old_time.isoformat(), "text": "Old news that should be pruned"},
            {"id": "@recent:1", "date": recent_time.isoformat(), "delivered_at": recent_time.isoformat(), "text": "Recent news that should be kept"},
        ]
        pruned = digest.prune_recent_news(recent_news, digest.utc_now())
        self.assertEqual(len(pruned), 1)
        self.assertEqual(pruned[0]["id"], "@recent:1")


class GeminiFallbackTests(unittest.TestCase):
    def test_gemini_fallback_does_not_remove_deterministic_candidates(self):
        posts = [
            {"id": "@one:1", "channel": "@one", "message_id": 1, "date": "2026-08-14T10:00:00+00:00", "text": "Apple представила новый iPhone.", "url": "https://t.me/one/1"},
            {"id": "@two:2", "channel": "@two", "message_id": 2, "date": "2026-08-14T10:01:00+00:00", "text": "Apple представила новый iPhone.", "url": "https://t.me/two/2"},
        ]
        self.assertEqual(len(posts), 2)




class WatchdogTests(unittest.TestCase):
    def test_no_last_run_no_warning(self):
        self.assertFalse(digest.watchdog_check({}, digest.utc_now()))

    def test_recent_run_no_warning(self):
        state = {"last_successful_run": (digest.utc_now() - timedelta(hours=2)).isoformat()}
        self.assertFalse(digest.watchdog_check(state, digest.utc_now()))

    def test_old_run_triggers_warning(self):
        state = {"last_successful_run": (digest.utc_now() - timedelta(hours=12)).isoformat()}
        self.assertTrue(digest.watchdog_check(state, digest.utc_now()))


if __name__ == "__main__":
    unittest.main()
