import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from digest import (
    collect_posts,
    filter_and_deduplicate,
    format_digest,
    is_probable_ad,
    telegram_chunks,
)


def post(text, source_id):
    return {
        "text": text,
        "date": "2026-07-30T10:00:00+00:00",
        "source_ids": [source_id],
        "sources": [
            {
                "id": source_id,
                "channel": "@testchannel",
                "url": "https://t.me/testchannel/1",
            }
        ],
    }


class DigestUtilityTests(unittest.TestCase):
    def test_obvious_ad_is_filtered(self):
        self.assertTrue(is_probable_ad("#реклама Получите скидку по промокоду"))
        self.assertFalse(is_probable_ad("Учёные опубликовали результаты исследования"))

    def test_duplicate_posts_keep_all_sources(self):
        text = "Учёные представили результаты большого исследования климата в Арктике."
        posts = filter_and_deduplicate([post(text, "@one:1"), post(text, "@two:7")])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["source_ids"], ["@one:1", "@two:7"])
        self.assertEqual(len(posts[0]["sources"]), 2)

    def test_digest_is_grouped_and_shows_sources(self):
        sources = {
            "@one:1": {"channel": "@one", "url": "https://t.me/one/1"},
            "@two:2": {"channel": "@two", "url": "https://t.me/two/2"},
        }
        text = format_digest(
            [
                {
                    "topic": "Наука",
                    "title": "Новый результат",
                    "summary": "Короткая проверенная сводка.",
                    "importance": 4,
                    "source_ids": ["@one:1", "@two:2"],
                }
            ],
            sources,
        )
        self.assertIn("Наука", text)
        self.assertIn("важность 4/5", text)
        self.assertIn("@one, @two", text)

    def test_telegram_messages_never_exceed_limit(self):
        chunks = list(telegram_chunks("слово\n" * 3_000))
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 3800 for chunk in chunks))

    def test_message_watermark_moves_only_for_a_successful_channel(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)

        class Client:
            def iter_messages(self, channel, min_id):
                if channel == "@broken":
                    raise OSError("network unavailable")
                self.assertEqual(min_id, 10)
                return iter(
                    [
                        SimpleNamespace(
                            id=11,
                            date=now,
                            message="Проверенная новость с достаточным количеством текста.",
                        )
                    ]
                )

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError(f"{left} != {right}")

        state = {
            "version": 2,
            "channels": {"@working": {"last_message_id": 10}, "@broken": {"last_message_id": 5}},
        }
        posts, next_state, failed = collect_posts(
            Client(), ["@working", "@broken"], state, now
        )
        self.assertEqual(posts[0]["source_ids"], ["@working:11"])
        self.assertEqual(next_state["channels"]["@working"]["last_message_id"], 11)
        self.assertEqual(next_state["channels"]["@broken"]["last_message_id"], 5)
        self.assertEqual(failed, ["@broken"])


if __name__ == "__main__":
    unittest.main()
