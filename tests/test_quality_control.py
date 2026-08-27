import unittest
from unittest.mock import patch

import run_digest


def post(source_id, text):
    channel, message_id = source_id.split(":")
    return {
        "id": source_id,
        "channel": channel,
        "message_id": int(message_id),
        "date": "2026-08-27T10:00:00+00:00",
        "text": text,
        "url": f"https://t.me/{channel.lstrip('@')}/{message_id}",
    }


class QualityControlTests(unittest.TestCase):
    def test_obvious_commercial_markers_are_removed_without_ai(self):
        posts = [
            post("@news:1", "Обычная новость о событии."),
            post("@ads:2", "Реклама: купите новый товар со скидкой."),
            post("@ads:3", "Промокод ABC123 действует до завтра."),
        ]
        with patch("run_digest.dp.make_ai_batches", return_value=[]):
            kept, removed = run_digest.review_all_ads(object(), posts)
        self.assertEqual([item["id"] for item in kept], ["@news:1"])
        self.assertEqual(removed, 2)

    def test_non_obvious_ads_are_sent_to_gemini(self):
        posts = [
            post("@news:1", "Министерство сообщило о новом решении."),
            post("@promo:2", "Попробуйте новый сервис — подробности по ссылке."),
        ]
        seen = []

        def fake_generate(_client, _prompt):
            seen.append(True)
            return {"ads": ["@promo:2"]}

        with patch("run_digest.dp.generate_json", side_effect=fake_generate):
            kept, removed = run_digest.review_all_ads(object(), posts)
        self.assertEqual([item["id"] for item in kept], ["@news:1"])
        self.assertEqual(removed, 1)
        self.assertTrue(seen)

    def test_global_semantic_pass_can_deduplicate_posts_in_one_stream(self):
        posts = [
            post("@one:1", "Компания представила новый продукт вчера."),
            post("@two:2", "Компания вчера представила новый продукт, сообщают источники."),
        ]
        with patch(
            "run_digest.dp.generate_json",
            return_value={"groups": [{"keep": "@one:1", "duplicates": ["@two:2"]}]},
        ):
            kept, removed = run_digest.enhanced_semantic_deduplicate(object(), posts)
        self.assertEqual([item["id"] for item in kept], ["@one:1"])
        self.assertEqual(removed, 1)


if __name__ == "__main__":
    unittest.main()
