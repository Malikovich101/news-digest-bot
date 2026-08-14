import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from google import genai
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

import digest

CHUNK_DELAY_SECONDS = 0.35


class DigestPipeline:
    """Single production pipeline with explicit state lifecycle and no runtime monkey patching."""

    def __init__(self, state_file=None):
        self.state_file = state_file or digest.STATE_FILE

    @staticmethod
    def parse_datetime(value, fallback=None):
        if not value:
            return fallback
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def normalize_text(text):
        return re.sub(r"\s+", " ", text or "").strip()

    def prune_recent_news(self, items, now):
        cutoff = now - timedelta(hours=digest.RECENT_NEWS_HOURS)
        valid = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            delivered_at = self.parse_datetime(item.get("delivered_at"))
            if delivered_at is None or delivered_at < cutoff:
                continue
            if not item.get("id") or not item.get("text") or not item.get("date"):
                continue
            valid.append({
                "id": item["id"],
                "date": item["date"],
                "delivered_at": delivered_at.isoformat(),
                "text": self.normalize_text(item["text"])[:digest.MAX_RECENT_NEWS_CHARS],
            })
        valid.sort(key=lambda item: item["delivered_at"], reverse=True)
        return valid

    def load_state(self):
        if not os.path.exists(self.state_file):
            return {"version": 5, "channels": {}, "delivered_ids": [], "delivered_chunks": [], "recent_news": []}
        with open(self.state_file, "r", encoding="utf-8") as file:
            raw = json.load(file)
        return {
            "version": 5,
            "channels": raw.get("channels", {}),
            "delivered_ids": list(dict.fromkeys(raw.get("delivered_ids", [])))[-digest.MAX_DELIVERED_IDS:],
            "delivered_chunks": list(dict.fromkeys(raw.get("delivered_chunks", [])))[-digest.MAX_DELIVERED_CHUNKS:],
            "recent_news": self.prune_recent_news(raw.get("recent_news", []), digest.utc_now()),
        }

    def save_state(self, state):
        state["version"] = 5
        state["delivered_ids"] = list(dict.fromkeys(state.get("delivered_ids", [])))[-digest.MAX_DELIVERED_IDS:]
        state["delivered_chunks"] = list(dict.fromkeys(state.get("delivered_chunks", [])))[-digest.MAX_DELIVERED_CHUNKS:]
        state["recent_news"] = self.prune_recent_news(state.get("recent_news", []), digest.utc_now())
        temp_file = self.state_file + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_file, self.state_file)

    def remember_delivered_news(self, state, posts, delivered_at):
        history = {
            item["id"]: item
            for item in state.get("recent_news", [])
            if isinstance(item, dict) and item.get("id")
        }
        for post in posts:
            history[post["id"]] = {
                "id": post["id"],
                "date": post["date"],
                "delivered_at": delivered_at.isoformat(),
                "text": self.normalize_text(post["text"])[:digest.MAX_RECENT_NEWS_CHARS],
            }
        state["recent_news"] = self.prune_recent_news(list(history.values()), delivered_at)

    def filter_posts_after_last_check(self, posts, state, replay_hours=0):
        """Reject overlap at the per-channel time boundary for normal runs."""
        if replay_hours:
            return posts, 0
        previous_channels = state.get("channels", {})
        filtered = []
        suppressed = 0
        for post in posts:
            channel_state = previous_channels.get(post.get("channel"), {})
            checked_at = self.parse_datetime(channel_state.get("last_checked_at"))
            post_date = self.parse_datetime(post.get("date"))
            if checked_at is not None and post_date is not None and post_date <= checked_at:
                suppressed += 1
                continue
            filtered.append(post)
        return filtered, suppressed

    def collect_posts(self, client, channels, state, now, replay_hours=0):
        posts = []
        channel_updates = {}
        failed_channels = []
        delivered_ids = set(state.get("delivered_ids", []))
        legacy_cutoff = self.parse_datetime(
            state.get("last_run"),
            now - timedelta(hours=digest.FIRST_RUN_LOOKBACK_HOURS),
        )
        replay_cutoff = now - timedelta(hours=replay_hours) if replay_hours else None
        for channel in channels:
            channel_state = state.get("channels", {}).get(channel, {})
            saved_message_id = int(channel_state.get("last_message_id", 0) or 0)
            min_id = 0 if replay_cutoff else saved_message_id
            cutoff = replay_cutoff or self.parse_datetime(channel_state.get("last_checked_at"), legacy_cutoff)
            newest_seen_id = saved_message_id
            channel_posts = []
            try:
                for message in client.iter_messages(channel, min_id=min_id):
                    if not min_id and message.date <= cutoff:
                        break
                    newest_seen_id = max(newest_seen_id, message.id)
                    if not message.message:
                        continue
                    post = {
                        "id": f"{channel}:{message.id}",
                        "channel": channel,
                        "message_id": message.id,
                        "date": message.date.isoformat(),
                        "text": message.message,
                        "url": digest.make_source_url(channel, message.id),
                    }
                    if not replay_cutoff and post["id"] in delivered_ids:
                        continue
                    channel_posts.append(post)
                posts.extend(channel_posts)
                channel_updates[channel] = {
                    "last_message_id": newest_seen_id,
                    "last_checked_at": now.isoformat(),
                }
            except Exception as error:
                failed_channels.append(channel)
                print(f"Не удалось прочитать {channel}: {error}")
        filtered, suppressed = self.filter_posts_after_last_check(
            posts, state, replay_hours=replay_hours
        )
        if suppressed:
            print(
                f"Digest time window: suppressed {suppressed} messages at or before "
                "the previous successful channel check."
            )
        for channel in failed_channels:
            channel_updates.pop(channel, None)
        return filtered, channel_updates, failed_channels

    def _history_candidates(self, current_posts, history):
        def tokens(text):
            normalized = self.normalize_text(text).lower()
            words = digest.WORD_RE.findall(digest.URL_RE.sub(" ", normalized))
            return {
                word[:5] if len(word) > 5 else word
                for word in words
                if word not in digest.COMMON_WORDS
            }

        scored_history = [(item, tokens(item.get("text", ""))) for item in history]
        selected = {}
        for post in current_posts:
            current = tokens(post["text"])
            if not current:
                continue
            ranked = []
            for item, other in scored_history:
                common = len(current & other)
                if common:
                    union = len(current | other)
                    ranked.append((common + (common / union if union else 0), common, item))
            ranked.sort(
                key=lambda row: (row[0], row[1], row[2].get("delivered_at", "")),
                reverse=True,
            )
            for _, _, item in ranked[:8]:
                selected[item["id"]] = item
        return sorted(selected.values(), key=lambda item: item.get("delivered_at", ""), reverse=True)

    def cross_run_deduplicate(self, client, posts, history):
        if not posts or not history:
            return posts, 0
        dropped = set()
        history = self.prune_recent_news(history, digest.utc_now())
        for batch in digest.make_ai_batches(posts, digest.MAX_PREVIEW_CHARS):
            candidates = self._history_candidates(batch, history)
            for start in range(0, len(candidates), digest.RECENT_NEWS_HISTORY_BATCH):
                response = digest.generate_json(
                    client,
                    digest.recent_news_prompt(
                        batch,
                        candidates[start:start + digest.RECENT_NEWS_HISTORY_BATCH],
                    ),
                )
                dropped.update(
                    digest.recent_news_repeat_ids(
                        response,
                        {post["id"] for post in batch},
                    )
                )
        return [post for post in posts if post["id"] not in dropped], len(dropped)

    def semantic_deduplicate(self, client, posts):
        if not posts:
            return posts, 0
        dropped = set()
        for batch in digest.make_ai_batches(posts, digest.MAX_PREVIEW_CHARS, overlap=20):
            active = [post for post in batch if post["id"] not in dropped]
            if len(active) < 2:
                continue
            payload = [
                {
                    "id": post["id"],
                    "date": post["date"],
                    "text": self.normalize_text(post["text"])[:digest.MAX_PREVIEW_CHARS],
                }
                for post in active
            ]
            prompt = f"""Ты определяешь только смысловые дубли новостей Telegram.

Найди только публикации об одном и том же конкретном событии. Не объединяй похожие темы,
новые факты и новые этапы истории. Если есть сомнение — не удаляй.

Если несколько публикаций действительно описывают одно и то же событие, сохраняй публикацию
с САМОЙ РАННЕЙ датой date. Более поздние публикации этого же события являются дублями.
Это обязательное правило: оно не позволяет удалению дубля создавать искусственный пробел
между соседними дайджестами.

Верни только JSON:
{{"groups":[{{"ids":["id1","id2"]}}]}}

Данные:
{json.dumps(payload, ensure_ascii=False)}"""
            response = digest.generate_json(client, prompt)
            groups = response.get("groups", []) if isinstance(response, dict) else []
            by_id = {post["id"]: post for post in active}
            for group in groups if isinstance(groups, list) else []:
                ids = [item for item in group.get("ids", []) if item in by_id] if isinstance(group, dict) else []
                ids = list(dict.fromkeys(ids))
                if len(ids) < 2:
                    continue
                earliest = min(ids, key=lambda item: by_id[item]["date"])
                dropped.update(item for item in ids if item != earliest)
        result = [post for post in posts if post["id"] not in dropped]
        return result, len(dropped)

    def send_telegram(self, token, chat_id, text, state, posts):
        delivered_chunks = set(state.get("delivered_chunks", []))
        chunks = list(digest.telegram_chunks(text))
        for index, chunk in enumerate(chunks):
            checkpoint = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            if checkpoint in delivered_chunks:
                continue
            last_error = None
            for attempt in range(digest.RETRY_ATTEMPTS):
                try:
                    response = requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data={"chat_id": chat_id, "text": chunk},
                        timeout=30,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok"):
                        raise requests.RequestException(
                            payload.get("description", "Telegram API rejected the message")
                        )
                    time.sleep(CHUNK_DELAY_SECONDS)
                    break
                except requests.RequestException as error:
                    last_error = error
                    if attempt < digest.RETRY_ATTEMPTS - 1:
                        time.sleep(2 ** attempt)
            else:
                raise RuntimeError(
                    f"Telegram delivery failed on chunk {index + 1}/{len(chunks)}: {last_error}"
                )
            delivered_chunks.add(checkpoint)
            state["delivered_chunks"] = list(delivered_chunks)[-digest.MAX_DELIVERED_CHUNKS:]
            delivered_ids = [
                post["id"]
                for post in posts
                if post.get("url") and post["url"] in chunk
            ]
            state["delivered_ids"] = list(
                dict.fromkeys(list(state.get("delivered_ids", [])) + delivered_ids)
            )[-digest.MAX_DELIVERED_IDS:]
            self.save_state(state)

        state["delivered_ids"] = list(
            dict.fromkeys(
                list(state.get("delivered_ids", [])) + [post["id"] for post in posts]
            )
        )[-digest.MAX_DELIVERED_IDS:]
        self.save_state(state)

    def run(self):
        digest.require_environment()
        now = digest.utc_now()
        state = self.load_state()
        channels = digest.load_channels()
        replay_hours = digest.replay_hours_from_environment()
        if not channels:
            raise RuntimeError("channels.txt is empty")

        with TelegramClient(
            StringSession(os.environ["TG_SESSION_STRING"]),
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
        ) as telegram_client:
            collected, channel_updates, failed_channels = self.collect_posts(
                telegram_client,
                channels,
                state,
                now,
                replay_hours,
            )
        if failed_channels and len(failed_channels) == len(channels):
            raise RuntimeError("All configured channels failed to load")

        collected.sort(key=lambda post: post["date"])
        posts, stats = digest.filter_and_deduplicate(collected)
        ai_unavailable = False
        confirmed_ads = semantic_duplicates = cross_run_duplicates = 0
        if posts:
            try:
                client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
                posts, confirmed_ads = digest.review_suspicious_ads(client, posts)
                if replay_hours == 0:
                    posts, cross_run_duplicates = self.cross_run_deduplicate(
                        client,
                        posts,
                        state.get("recent_news", []),
                    )
                posts, semantic_duplicates = self.semantic_deduplicate(client, posts)
            except RuntimeError as error:
                ai_unavailable = True
                print(f"Gemini unavailable, sending without AI filtering: {error}")

        digest_text = digest.format_digest(
            posts,
            stats,
            semantic_duplicates,
            confirmed_ads,
            ai_unavailable,
            cross_run_duplicates,
        )
        if not posts and not ai_unavailable:
            digest_text = "❗❗❗❗❗❗\n🗞 За этот период новых подходящих новостей не было."
        if failed_channels:
            digest_text += "\n\n⚠️ Не удалось проверить: " + ", ".join(failed_channels)

        self.send_telegram(
            os.environ["TG_BOT_TOKEN"],
            os.environ["TG_CHAT_ID"],
            digest_text,
            state,
            posts,
        )

        if replay_hours == 0:
            self.remember_delivered_news(state, posts, digest.utc_now())
            state["channels"].update(channel_updates)
            self.save_state(state)
        else:
            state["delivered_ids"] = []
            self.save_state(state)

        print(
            f"Delivered {len(posts)} original posts from {len(collected)} collected "
            f"(semantic_duplicates={semantic_duplicates}, "
            f"cross_run_duplicates={cross_run_duplicates}, "
            f"confirmed_ads={confirmed_ads}, replay_hours={replay_hours})"
        )
