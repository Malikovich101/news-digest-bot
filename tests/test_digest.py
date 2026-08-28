import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from digest import MODELS, generate_json, is_probable_ad, is_suspicious_ad, filter_and_deduplicate, review_suspicious_ads, ad_ids_from_response, duplicate_ids_from_response, candidate_clusters, cross_run_semantic_deduplicate, format_digest, telegram_chunks


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

    def test_gemini_can_remove_only_confirmed_ads(self):
        posts = [post("Купите курс со скидкой 50% прямо сейчас!", "@one:1"), post("Компания снизила цены на лекарства, пациенты получат помощь дешевле.", "@two:2")]
        class Models:
            def generate_content(self, model, contents, config): return SimpleNamespace(text='{"ads":["@one:1"]}')
        kept, confirmed = review_suspicious_ads(SimpleNamespace(models=Models()), posts)
        self.assertEqual([item["id"] for item in kept], ["@two:2"])
        self.assertEqual(confirmed, 1)

    def test_responses_accept_only_known_ids(self):
        self.assertEqual(ad_ids_from_response({"ads": ["@one:1", "wrong", 123]}, {"@one:1", "@two:2"}), {"@one:1"})
        self.assertEqual(duplicate_ids_from_response({"groups": [{"keep": "@one:1", "duplicates": ["@two:2", "wrong"]}]}, {"@one:1", "@two:2"}), {"@two:2"})

    def test_candidate_clusters_find_reposts(self):
        posts = [post("Telegram вновь появился в App Store, доступ восстановлен.", "@one:1"), post("Telegram вернули в App Store — доступ восстановлен.", "@two:2"), post("Telegram восстановили в AppStore.", "@three:3"), post("Совершенно другая новость о космосе и телескопе.", "@four:4")]
        clusters = candidate_clusters(posts)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({item["id"] for item in clusters[0]}, {"@one:1", "@two:2", "@three:3"})

    def test_format_keeps_original_text(self):
        original = "Первая строка\n\n  Вторая строка без изменений."
        text = format_digest([post(original, "@one:1")], {"source_posts": 3, "short": 0, "ads": 1, "ad_review": 2, "python_duplicates": 1}, semantic_duplicates=0, confirmed_ads=1, cross_run_duplicates=2)
        self.assertIn(original, text)
        self.assertIn("оригинальных публикаций: 1", text)

    def test_telegram_limit_and_chunking(self):
        chunks = list(telegram_chunks("слово\n" * 3000))
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))

    def test_cross_run_semantic_memory_filters_duplicate_event(self):
        current = [post("Apple представила новый iPhone X на презентации.", "@current:10"), post("Компания анонсировала новый тариф для мобильной связи.", "@current:11")]
        history = [{"id": "@old:1", "date": "2026-07-30T09:00:00+00:00", "delivered_at": "2026-07-30T09:10:00+00:00", "text": "Apple представила новый iPhone X на презентации."}]
        class Models:
            def generate_content(self, model, contents, config): return SimpleNamespace(text='{"repeats":["@current:10"]}')
        kept, dropped = cross_run_semantic_deduplicate(SimpleNamespace(models=Models()), current, history)
        self.assertEqual([p["id"] for p in kept], ["@current:11"])
        self.assertEqual(dropped, 1)

    def test_generate_json_uses_fallback_model(self):
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
        self.assertEqual(models.calls[:3], [MODELS[0]] * 3)
        self.assertEqual(models.calls[-1], MODELS[1])


if __name__ == "__main__": unittest.main()
