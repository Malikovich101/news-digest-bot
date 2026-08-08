import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from digest import (
    MODELS,
    candidate_clusters,
    collect_posts,
    duplicate_ids_from_response,
    filter_and_deduplicate,
    format_digest,
    generate_json,
    is_probable_ad,
    telegram_chunks,
)


def post(text, source_id="@test:1", url="https://t.me/test/1"):
    channel, message_id = source_id.split(":")
    return {
        "id": source_id,
        "channel": channel,
        "message_id": int(message_id),
        "date": "2026-07-30T10:00:00+00:00",
        "text": text,
        "url": url,
    }


class DigestUtilityTests(unittest.TestCase):
    def test_obvious_ad_is_filtered(self):
        self.assertTrue(is_probable_ad("#реклама Получите скидку по промокоду"))
        self.assertFalse(is_probable_ad("Учёные опубликовали результаты исследования"))

    def test_python_removes_only_exact_text_duplicates(self):
        text = "Учёные представили результаты большого исследования климата в Арктике."
        kept, stats = filter_and_deduplicate([post(text, "@one:1"), post(text, "@two:7")])
        self.assertEqual([item["id"] for item in kept], ["@one:1"])
        self.assertEqual(stats["python_duplicates"], 1)

    def test_link_only_repost_is_removed(self):
        link = "https://example.com/news/1"
        kept, stats = filter_and_deduplicate(
            [
                post(f"Подробности и исходный материал по ссылке {link}", "@one:1", link),
                post(f"Короткий комментарий и та же ссылка на материал {link}", "@two:7", link),
            ]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["python_duplicates"], 1)

    def test_model_can_remove_only_valid_duplicate_ids(self):
        response = {"groups": [{"keep": "@one:1", "duplicates": ["@two:2", "wrong"]}]}
        dropped = duplicate_ids_from_response(response, {"@one:1", "@two:2"})
        self.assertEqual(dropped, {"@two:2"})

    def test_candidate_clusters_find_app_store_reposts(self):
        posts = [
            post("Telegram вновь появился в App Store, доступ к приложению восстановлен.", "@one:1"),
            post("Telegram вернули в App Store — доступ к приложению восстановлен.", "@two:2"),
            post("Telegram восстановили в AppStore.", "@three:3"),
            post("Совершенно другая новость о космосе и телескопе.", "@four:4"),
        ]
        clusters = candidate_clusters(posts)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({item["id"] for item in clusters[0]}, {"@one:1", "@two:2", "@three:3"})

    def test_digest_keeps_original_text_and_shows_clear_counts(self):
        original = "Первая строка\n\n  Вторая строка без изменений."
        text = format_digest(
            [post(original, "@one:1")],
            {"source_posts": 3, "short": 0, "ads": 1, "python_duplicates": 1},
            semantic_duplicates=0,
        )
        self.assertIn(original, text)
        self.assertIn("точных повторов: 1", text)
        self.assertIn("смысловых повторов: 0", text)
        self.assertIn("оригинальных публикаций: 1", text)

    def test_telegram_messages_never_exceed_limit(self):
        chunks = list(telegram_chunks("слово\n" * 3_000))
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 3800 for chunk in chunks))

    def test_watermark_moves_only_for_a_successful_channel(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)

        class Client:
            def iter_messages(self, channel, min_id):
                if channel == "@broken":
                    raise OSError("network unavailable")
                if min_id != 10:
                    raise AssertionError(f"{min_id} != 10")
                return iter([
                    SimpleNamespace(
                        id=11,
                        date=now,
                        message="Проверенная новость с достаточным количеством текста.",
                    )
                ])

        state = {"version": 2, "channels": {"@working": {"last_message_id": 10}, "@broken": {"last_message_id": 5}}}
        posts, next_state, failed = collect_posts(Client(), ["@working", "@broken"], state, now)
        self.assertEqual(posts[0]["id"], "@working:11")
        self.assertEqual(next_state["channels"]["@working"]["last_message_id"], 11)
        self.assertEqual(next_state["channels"]["@broken"]["last_message_id"], 5)
        self.assertEqual(failed, ["@broken"])

    def test_failed_channel_does_not_leak_partial_posts(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)

        class Client:
            def iter_messages(self, channel, min_id):
                def broken():
                    yield SimpleNamespace(
                        id=11,
                        date=now,
                        message="Частично прочитанная новость, которую нельзя считать доставленной.",
                    )
                    raise OSError("network unavailable")
                return broken()

        state = {"version": 2, "channels": {"@broken": {"last_message_id": 10}}}
        posts, next_state, failed = collect_posts(Client(), ["@broken"], state, now)
        self.assertEqual(posts, [])
        self.assertEqual(next_state["channels"]["@broken"]["last_message_id"], 10)
        self.assertEqual(failed, ["@broken"])

    def test_delivered_id_is_not_recollected_on_normal_run(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)

        class Client:
            def iter_messages(self, channel, min_id):
                return iter([
                    SimpleNamespace(id=11, date=now, message="Уже доставленная новость не должна прийти повторно."),
                    SimpleNamespace(id=12, date=now, message="Новая новость должна попасть в следующий дайджест."),
                ])

        state = {"version": 3, "channels": {"@test": {"last_message_id": 10}}, "delivered_ids": ["@test:11"]}
        posts, _, failed = collect_posts(Client(), ["@test"], state, now)
        self.assertEqual([item["id"] for item in posts], ["@test:12"])
        self.assertEqual(failed, [])

    def test_replay_does_not_use_delivered_id_filter(self):
        now = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)

        class Client:
            def iter_messages(self, channel, min_id):
                return iter([SimpleNamespace(id=11, date=now, message="Эту новость нужно повторно собрать в режиме replay.")])

        state = {"version": 3, "channels": {"@test": {"last_message_id": 10}}, "delivered_ids": ["@test:11"]}
        posts, _, failed = collect_posts(Client(), ["@test"], state, now, replay_hours=1)
        self.assertEqual([item["id"] for item in posts], ["@test:11"])
        self.assertEqual(failed, [])

    def test_model_fallback_is_used_after_temporary_failures(self):
        class Models:
            def __init__(self):
                self.calls = []

            def generate_content(self, model, contents, config):
                self.calls.append((model, config))
                if model == MODELS[0]:
                    raise RuntimeError("503 UNAVAILABLE")
                return SimpleNamespace(text='{"groups": []}')

        client = SimpleNamespace(models=Models())
        with patch("digest.time.sleep"):
            response = generate_json(client, "test")

        self.assertEqual(response, {"groups": []})
        self.assertEqual([model for model, _ in client.models.calls].count(MODELS[0]), 2)
        self.assertEqual(client.models.calls[-1][0], MODELS[1])
        self.assertNotIn("temperature", client.models.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
