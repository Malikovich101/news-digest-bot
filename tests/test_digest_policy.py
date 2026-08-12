import unittest
from unittest.mock import patch
from types import SimpleNamespace

import digest
import digest_policy


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


class DigestPolicyTests(unittest.TestCase):
    def test_duplicate_event_keeps_earliest_publication(self):
        posts = [
            post("@early:1", "2026-08-12T21:10:00+00:00", "Компания представила новый продукт вчера вечером."),
            post("@late:2", "2026-08-13T01:18:00+00:00", "Компания представила новый продукт вчера вечером по данным канала."),
        ]
        response = {"groups": [{"keep": "@late:2", "duplicates": ["@early:1"]}]}
        dropped = digest_policy._drop_groups(response, posts, {p["id"] for p in posts})
        self.assertEqual(dropped, {"@late:2"})

    def test_semantic_policy_does_not_restore_posts_for_time_coverage(self):
        digest_policy.install(digest)
        posts = [
            post("@early:1", "2026-08-12T21:10:00+00:00", "Важная новость о запуске нового сервиса компании."),
            post("@late:2", "2026-08-13T01:18:00+00:00", "Важная новость о запуске нового сервиса компании."),
        ]

        class Models:
            def generate_content(self, model, contents, config):
                return SimpleNamespace(text='{"groups":[{"keep":"@late:2","duplicates":["@early:1"]}]}')

        client = SimpleNamespace(models=Models())
        with patch.object(digest, "generate_json", side_effect=lambda client, prompt: {"groups": [{"keep": "@late:2", "duplicates": ["@early:1"]]}]):
            kept, dropped = digest.semantic_deduplicate(client, posts)

        self.assertEqual([item["id"] for item in kept], ["@early:1"])
        self.assertEqual(dropped, 1)


if __name__ == "__main__":
    unittest.main()
