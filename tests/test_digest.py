import unittest
from types import SimpleNamespace
from unittest.mock import patch

from digest import MODELS, generate_json, is_probable_ad, is_suspicious_ad, filter_and_deduplicate, review_suspicious_ads, format_digest, telegram_chunks


def post(text, source_id="@test:1", url="https://t.me/test/1"):
    channel, message_id = source_id.split(":")
    return {"id": source_id, "channel": channel, "message_id": int(message_id), "date": "2026-07-30T10:00:00+00:00", "text": text, "url": url}


class DigestUtilityTests(unittest.TestCase):
    def test_explicit_ad_is_filtered_without_ai(self):
        self.assertTrue(is_probable_ad("#реклама Получите скидку по промокоду"))
        self.assertFalse(is_probable_ad("Учёные опубликовали результаты исследования"))

    def test_promo_words_are_suspicious_not_deterministic_ads(self):
        kept, stats = filter_and_deduplicate([post("Компания объявила скидку на билеты после изменения цен.")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["ads"], 0)
        self.assertEqual(stats["ad_review"], 1)
        self.assertTrue(is_suspicious_ad(kept[0]["text"]))

    def test_literal_repost_with_different_link_and_signature_is_filtered(self):
        posts = [post("Компания представила новый продукт. https://example.com/a @channelone", "@one:1"), post("Компания представила новый продукт. https://example.com/b @channeltwo", "@two:2")]
        kept, stats = filter_and_deduplicate(posts)
        self.assertEqual([item["id"] for item in kept], ["@one:1"])
        self.assertEqual(stats["python_duplicates"], 1)

    def test_gemini_can_remove_only_confirmed_ads(self):
        posts = [post("Купите курс со скидкой 50% прямо сейчас!", "@one:1"), post("Компания снизила цены на лекарства, пациенты получат помощь дешевле.", "@two:2")]
        class Models:
            def generate_content(self, model, contents, config): return SimpleNamespace(text='{"ads":["@one:1"]}')
        kept, confirmed, calls = review_suspicious_ads(SimpleNamespace(models=Models()), posts)
        self.assertEqual([item["id"] for item in kept], ["@two:2"])
        self.assertEqual(confirmed, 1)
        self.assertEqual(calls, 1)

    def test_format_keeps_original_text(self):
        original = "Первая строка\n\n  Вторая строка без изменений."
        text = format_digest([post(original, "@one:1")], {"source_posts": 3, "short": 0, "ads": 1, "ad_review": 2, "python_duplicates": 1}, semantic_duplicates=0, confirmed_ads=1)
        self.assertIn(original, text)
        self.assertIn("оригинальных публикаций: 1", text)

    def test_telegram_limit_and_chunking(self):
        chunks = list(telegram_chunks("слово\n" * 3000))
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))

    def test_generate_json_uses_limited_retry_and_fallback_model(self):
        class Models:
            def __init__(self): self.calls=[]
            def generate_content(self, model, contents, config):
                self.calls.append(model)
                if model == MODELS[0]: raise RuntimeError("503 UNAVAILABLE")
                return SimpleNamespace(text='{"groups": []}')
        models = Models()
        with patch("digest.time.sleep"):
            result = generate_json(SimpleNamespace(models=models), "test")
        self.assertEqual(result, {"groups": []})
        self.assertEqual(models.calls[:2], [MODELS[0]] * 2)
        self.assertEqual(models.calls[-1], MODELS[1])


if __name__ == "__main__": unittest.main()
