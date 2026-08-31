from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from google import genai
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    InviteHashEmptyError,
    RPCError,
    UserDeactivatedError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

CHUNK_DELAY_SECONDS = 0.35
STATE_FILE = "state.json"
CHANNELS_FILE = "channels.txt"
RUN_HISTORY_FILE = "run_history.jsonl"
MODELS = ("gemini-3.5-flash", "gemini-3.5-flash-lite")
FIRST_RUN_LOOKBACK_HOURS = 9
MIN_TEXT_LENGTH = 20
MAX_MODEL_POST_CHARS = 1_600
MAX_MODEL_INPUT_CHARS = 48_000
RETRY_ATTEMPTS = 2
PERM_TIMEZONE = timezone(timedelta(hours=5))
MAX_DELIVERED_IDS = 5_000
TELEGRAM_MESSAGE_LIMIT = 3900
CHANNEL_FETCH_DELAY_SECONDS = 0.5
AI_MAX_CALLS = 4
AI_CALL_TIMEOUT_SECONDS = 45

AD_MARKERS = (
    "#реклама", "erid", "промокод", "рекламная интеграция",
    "на правах рекламы", "партнёрский материал", "партнерский материал",
)
PROMO_PATTERNS = (
    r"\bподпис\w*\b", r"\bрозыгрыш\w*\b", r"\bскидк\w*\b",
    r"\bкуп\w*\b", r"\bзаказ\w*\b", r"\bрегистр\w*\b",
)
URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
WORD_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
COMMON_WORDS = {"это", "как", "что", "для", "или", "при", "после", "через", "также", "будет", "были", "была", "есть", "еще", "новый", "новая", "новости", "сообщил", "сообщили", "компания", "сегодня", "теперь", "который"}


def utc_now():
    return datetime.now(timezone.utc)


def analysis_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_datetime(value, fallback=None):
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return fallback


def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        return []
    with open(CHANNELS_FILE, "r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip() and not line.strip().startswith("#")]


def normalized_duplicate_text(text):
    value = analysis_text(text).lower()
    value = URL_RE.sub(" ", value)
    value = re.sub(r"@[a-z0-9_]{5,}", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"#[\wа-яё]+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def is_probable_ad(text):
    normalized = analysis_text(text).lower()
    return any(marker in normalized for marker in AD_MARKERS)


def is_suspicious_ad(text):
    normalized = analysis_text(text).lower()
    if is_probable_ad(normalized):
        return False
    return any(re.search(pattern, normalized) for pattern in PROMO_PATTERNS)


def extract_urls(text):
    return {url.lower().rstrip(".,!?;:)]") for url in URL_RE.findall(text)}


def make_source_url(channel, message_id):
    return f"https://t.me/{channel.lstrip('@')}/{message_id}"


def extract_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise
        data = json.loads(match.group())
    if not isinstance(data, dict):
        raise ValueError("Gemini returned an unexpected JSON structure")
    return data


def generate_json(client, prompt):
    last_error = None
    for model in MODELS:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"response_mime_type": "application/json", "http_options": {"timeout": AI_CALL_TIMEOUT_SECONDS * 1000}},
                )
                return extract_json_object(response.text)
            except Exception as error:
                last_error = error
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(1)
    raise RuntimeError(f"Gemini unavailable: {last_error}")


def filter_and_deduplicate(posts):
    kept = []
    seen_text = set()
    seen_link_posts = set()
    stats = {"source_posts": len(posts), "short": 0, "ads": 0, "ad_review": 0, "python_duplicates": 0}
    for post in posts:
        comparable = analysis_text(post["text"])
        text_without_urls = URL_RE.sub("", comparable).strip()
        if len(text_without_urls) < MIN_TEXT_LENGTH:
            stats["short"] += 1
            continue
        if is_probable_ad(comparable):
            stats["ads"] += 1
            continue
        if is_suspicious_ad(comparable):
            stats["ad_review"] += 1
        exact_key = normalized_duplicate_text(comparable)
        links = extract_urls(comparable)
        if exact_key in seen_text or (len(text_without_urls) <= 80 and bool(links & seen_link_posts)):
            stats["python_duplicates"] += 1
            continue
        seen_text.add(exact_key)
        if len(text_without_urls) <= 80:
            seen_link_posts.update(links)
        kept.append(post)
    return kept, stats


def make_ai_batches(posts, text_limit, max_batches=2):
    batches, current, current_size = [], [], 0
    for post in posts:
        item_size = len(json.dumps({"id": post["id"], "text": analysis_text(post["text"])[:text_limit]}, ensure_ascii=False))
        if current and current_size + item_size > MAX_MODEL_INPUT_CHARS:
            batches.append(current)
            if len(batches) >= max_batches:
                break
            current, current_size = [], 0
        current.append(post)
        current_size += item_size
    if current and len(batches) < max_batches:
        batches.append(current)
    return batches


def duplicate_prompt(posts):
    payload = [{"id": post["id"], "text": analysis_text(post["text"])[:MAX_MODEL_POST_CHARS]} for post in posts]
    return f"""Ты определяешь только смысловые дубли новостей. Найди группы сообщений об одном и том же конкретном событии. Не объединяй новые развития одной истории. При сомнении оставь обе публикации.
Верни только JSON: {{\"groups\":[{{\"keep\":\"id\",\"duplicates\":[\"id\"]}}]}}
Сообщения:
{json.dumps(payload, ensure_ascii=False)}"""


def semantic_deduplicate(client, posts):
    if len(posts) < 2:
        return posts, 0, 0
    dropped, calls = set(), 0
    for batch in make_ai_batches(posts, MAX_MODEL_POST_CHARS, max_batches=2):
        if len(batch) < 2 or calls >= AI_MAX_CALLS:
            continue
        calls += 1
        response = generate_json(client, duplicate_prompt(batch))
        allowed = {post["id"] for post in batch}
        groups = response.get("groups", [])
        used = set()
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                keep = group.get("keep")
                duplicates = group.get("duplicates", [])
                if keep not in allowed or keep in used or not isinstance(duplicates, list):
                    continue
                valid = [item for item in duplicates if item in allowed and item != keep and item not in used]
                if valid:
                    used.add(keep)
                    used.update(valid)
                    dropped.update(valid)
    return [post for post in posts if post["id"] not in dropped], len(dropped), calls


def ad_review_prompt(posts):
    payload = [{"id": post["id"], "text": analysis_text(post["text"])[:MAX_MODEL_POST_CHARS]} for post in posts]
    return f"""Ты классифицируешь Telegram-публикации только на предмет рекламы. Рекламой считается публикация, основная цель которой — продвигать товар, услугу, бренд, платное мероприятие, промокод или коммерческое предложение. При сомнении оставь публикацию.
Верни только JSON: {{\"ads\":[\"id\"]}}
Сообщения:
{json.dumps(payload, ensure_ascii=False)}"""


def review_suspicious_ads(client, posts):
    suspicious = [post for post in posts if is_suspicious_ad(post["text"])]
    if not suspicious:
        return posts, 0, 0
    dropped, calls = set(), 0
    for batch in make_ai_batches(suspicious, MAX_MODEL_POST_CHARS, max_batches=2):
        calls += 1
        response = generate_json(client, ad_review_prompt(batch))
        allowed = {post["id"] for post in batch}
        ads = response.get("ads", [])
        if isinstance(ads, list):
            dropped.update(item for item in ads if item in allowed)
    return [post for post in posts if post["id"] not in dropped], len(dropped), calls


def format_post_time(value):
    return datetime.fromisoformat(value).astimezone(PERM_TIMEZONE).strftime("%d.%m %H:%M")


def render_post(post):
    return "\n".join(["────────────", f"🕒 {format_post_time(post['date'])} · {post['channel']}", post["text"], f"Источник: {post['url']}"])


def format_digest(posts, stats, semantic_duplicates, confirmed_ads=0, ai_unavailable=False):
    lines = ["❗❗❗❗❗❗", "🗞 Оригинальные новости", f"📊 Постов с текстом: {stats['source_posts']}; явной рекламы: {stats['ads']}; проверено Gemini: {stats.get('ad_review', 0)}; рекламы подтверждено Gemini: {confirmed_ads}; отсеяно коротких: {stats['short']}; точных повторов: {stats['python_duplicates']}; смысловых повторов: {semantic_duplicates}; оригинальных публикаций: {len(posts)}", "Каждый текст ниже — исходная публикация канала без пересказа и сокращения."]
    if ai_unavailable:
        lines.append("⚠️ Gemini недоступен: отправлены безопасные детерминированные кандидаты без AI-дедупликации.")
    lines.extend(render_post(post) for post in posts)
    return "\n".join(lines).strip()


def telegram_chunks(text, limit=TELEGRAM_MESSAGE_LIMIT):
    while text:
        if len(text) <= limit:
            yield text
            return
        boundary = text.rfind("\n────────────", 0, limit)
        if boundary >= limit // 2:
            yield text[:boundary].rstrip()
            text = text[boundary:].lstrip()
            continue
        boundary = text.rfind("\n", 0, limit)
        if boundary >= limit // 2:
            yield text[:boundary].rstrip()
            text = text[boundary:].lstrip()
            continue
        yield text[:limit]
        text = text[limit:]


def chunk_checkpoint_id(chunk):
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()


def require_environment():
    required = ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING", "TG_BOT_TOKEN", "TG_CHAT_ID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required secrets: {', '.join(missing)}")


def migrate_state(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {"version": 8, "channels": raw.get("channels", {}), "pending_posts": raw.get("pending_posts", {}) or {}, "delivered_ids": list(dict.fromkeys(raw.get("delivered_ids", [])))[-MAX_DELIVERED_IDS:], "delivery_receipts": raw.get("delivery_receipts", {}) or {}, "recent_news": raw.get("recent_news", []) or [], "last_successful_run": raw.get("last_successful_run"), "completed_slots": raw.get("completed_slots", {}) or {}}


def prune_recent_news(recent_news, now):
    cutoff = now - timedelta(hours=72)
    valid = []
    for item in recent_news or []:
        if not isinstance(item, dict):
            continue
        delivered = parse_datetime(item.get("delivered_at"), None)
        if delivered is None or delivered < cutoff or not item.get("id") or not item.get("text") or not item.get("date"):
            continue
        valid.append({"id": item["id"], "date": item["date"], "delivered_at": delivered.isoformat(), "text": analysis_text(item["text"])[:700]})
    valid.sort(key=lambda item: item["delivered_at"], reverse=True)
    return valid[:500]


def prune_state(state, now):
    state["pending_posts"] = {key: value for key, value in (state.get("pending_posts", {}) or {}).items() if isinstance(value, dict) and parse_datetime(value.get("collected_at"), None) and parse_datetime(value.get("collected_at"), None) >= now - timedelta(hours=48)}
    state["delivery_receipts"] = {key: value for key, value in (state.get("delivery_receipts", {}) or {}).items() if isinstance(value, dict) and parse_datetime(value.get("sent_at"), None) and parse_datetime(value.get("sent_at"), None) >= now - timedelta(hours=72)}
    state["recent_news"] = prune_recent_news(state.get("recent_news", []), now)
    state["completed_slots"] = {slot: completed_at for slot, completed_at in (state.get("completed_slots", {}) or {}).items() if parse_datetime(completed_at, None) and parse_datetime(completed_at, None) >= now - timedelta(days=7)}
    state["version"] = 8
    return state


class DigestPipeline:
    def __init__(self, state_file=None):
        self.state_file = state_file or STATE_FILE

    @staticmethod
    def parse_datetime(value, fallback=None):
        return parse_datetime(value, fallback)

    @staticmethod
    def semantic_deduplicate(client, posts):
        kept, dropped, _ = semantic_deduplicate(client, posts)
        return kept, dropped

    def load_state(self):
        if not os.path.exists(self.state_file):
            return migrate_state({})
        with open(self.state_file, "r", encoding="utf-8") as file:
            return prune_state(migrate_state(json.load(file)), utc_now())

    def save_state(self, state):
        state = prune_state(migrate_state(state), utc_now())
        temp_file = self.state_file + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_file, self.state_file)

    def collect_posts(self, client, channels, state, now):
        posts, updates, failed = [], {}, []
        delivered_ids = set(state.get("delivered_ids", []))
        pending_ids = set(state.get("pending_posts", {}))
        legacy_cutoff = parse_datetime(state.get("last_successful_run"), now - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS))
        for index, channel in enumerate(channels):
            channel_state = state.get("channels", {}).get(channel, {})
            saved_id = int(channel_state.get("last_message_id", 0) or 0)
            cutoff = parse_datetime(channel_state.get("last_checked_at"), legacy_cutoff)
            newest_seen_id = saved_id
            try:
                for message in client.iter_messages(channel, min_id=saved_id):
                    if saved_id and message.date <= cutoff:
                        break
                    newest_seen_id = max(newest_seen_id, message.id)
                    if not message.message:
                        continue
                    post = {"id": f"{channel}:{message.id}", "channel": channel, "message_id": message.id, "date": message.date.isoformat(), "text": message.message, "url": make_source_url(channel, message.id)}
                    if post["id"] not in delivered_ids and post["id"] not in pending_ids:
                        posts.append(post)
                updates[channel] = {"last_message_id": newest_seen_id, "last_checked_at": now.isoformat()}
                if index < len(channels) - 1:
                    time.sleep(CHANNEL_FETCH_DELAY_SECONDS)
            except FloodWaitError as error:
                time.sleep(min(error.seconds, 30) + 1)
                failed.append(channel)
            except (UserDeactivatedError, UsernameNotOccupiedError, InviteHashEmptyError, ChannelPrivateError, RPCError) as error:
                print(f"Telegram error for {channel}: {error}")
                failed.append(channel)
            except Exception as error:
                print(f"Failed to read {channel}: {error}")
                failed.append(channel)
        for channel in failed:
            updates.pop(channel, None)
        return posts, updates, failed

    def send_telegram(self, token, chat_id, text, state, posts, rendered_chunks=None):
        records = rendered_chunks or [{"id": chunk_checkpoint_id(chunk), "text": chunk, "post_ids": [post["id"] for post in posts if post["url"] in chunk]} for chunk in telegram_chunks(text)]
        receipts = state.setdefault("delivery_receipts", {})
        for index, record in enumerate(records):
            checkpoint = record["id"]
            if checkpoint in receipts:
                continue
            last_error = None
            for attempt in range(RETRY_ATTEMPTS):
                try:
                    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": record["text"]}, timeout=30)
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok"):
                        raise requests.RequestException(payload.get("description", "Telegram rejected message"))
                    break
                except requests.RequestException as error:
                    last_error = error
                    if attempt < RETRY_ATTEMPTS - 1:
                        time.sleep(1)
            else:
                raise RuntimeError(f"Telegram delivery failed on chunk {index + 1}/{len(records)}: {last_error}")
            receipts[checkpoint] = {"sent_at": utc_now().isoformat(), "post_ids": record.get("post_ids", [])}
            for post_id in record.get("post_ids", []):
                state.setdefault("pending_posts", {}).pop(post_id, None)
                state.setdefault("delivered_ids", []).append(post_id)
            state["delivered_ids"] = list(dict.fromkeys(state["delivered_ids"]))[-MAX_DELIVERED_IDS:]
            self.save_state(state)
            time.sleep(CHUNK_DELAY_SECONDS)

    def run(self):
        started_at = utc_now()
        state = self.load_state()
        slot_id = os.environ.get("DIGEST_SLOT_ID", "").strip()
        if slot_id and slot_id in state.get("completed_slots", {}):
            return {"status": "skipped", "slot_id": slot_id, "reason": "slot already completed"}
        require_environment()
        channels = load_channels()
        if not channels:
            raise RuntimeError("channels.txt is empty")
        with TelegramClient(StringSession(os.environ["TG_SESSION_STRING"]), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as telegram_client:
            collected, channel_updates, failed_channels = self.collect_posts(telegram_client, channels, state, utc_now())
        if failed_channels and len(failed_channels) == len(channels):
            raise RuntimeError("All configured channels failed to load")
        posts, stats = filter_and_deduplicate(collected)
        ai_unavailable = False
        confirmed_ads = semantic_duplicates = 0
        gemini_calls = 0
        if posts and os.environ.get("GEMINI_API_KEY"):
            try:
                client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
                posts, confirmed_ads, calls = review_suspicious_ads(client, posts)
                gemini_calls += calls
                if posts and gemini_calls < AI_MAX_CALLS:
                    posts, semantic_duplicates, calls = semantic_deduplicate(client, posts)
                    gemini_calls += calls
            except Exception as error:
                ai_unavailable = True
                print(f"Gemini optional processing failed: {error}")
        elif posts:
            ai_unavailable = True
        if posts:
            for post in posts:
                state.setdefault("pending_posts", {})[post["id"]] = {**post, "collected_at": started_at.isoformat()}
            self.save_state(state)
            digest_text = format_digest(posts, stats, semantic_duplicates, confirmed_ads, ai_unavailable)
        else:
            digest_text = "❗❗❗❗❗❗\n🗞 За этот период новых подходящих новостей не было."
        if failed_channels:
            digest_text += "\n\n⚠️ Не удалось проверить: " + ", ".join(failed_channels)
        self.send_telegram(os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"], digest_text, state, posts)
        state["channels"].update(channel_updates)
        state["last_successful_run"] = utc_now().isoformat()
        if slot_id:
            state.setdefault("completed_slots", {})[slot_id] = state["last_successful_run"]
        delivered_at = state["last_successful_run"]
        state["recent_news"] = prune_recent_news(state.get("recent_news", []) + [{"id": post["id"], "date": post["date"], "delivered_at": delivered_at, "text": analysis_text(post["text"])[:700]} for post in posts], utc_now())
        self.save_state(state)
        return {"status": "success", "slot_id": slot_id or None, "source_posts": stats["source_posts"], "delivered_posts": len(posts), "explicit_ads": stats["ads"], "semantic_duplicates": semantic_duplicates, "failed_channels": failed_channels, "ai_unavailable": ai_unavailable}


def append_run_history(entry):
    rows = []
    if os.path.exists(RUN_HISTORY_FILE):
        with open(RUN_HISTORY_FILE, "r", encoding="utf-8") as source:
            rows = [line for line in source if line.strip()][-199:]
    rows.append(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    with open(RUN_HISTORY_FILE, "w", encoding="utf-8") as output:
        output.writelines(rows)


def main():
    started_at = utc_now()
    try:
        result = DigestPipeline().run()
        append_run_history({"timestamp": utc_now().isoformat(), "started_at": started_at.isoformat(), "run_id": os.environ.get("GITHUB_RUN_ID"), **result})
    except Exception as error:
        append_run_history({"timestamp": utc_now().isoformat(), "started_at": started_at.isoformat(), "run_id": os.environ.get("GITHUB_RUN_ID"), "status": "failed", "slot_id": os.environ.get("DIGEST_SLOT_ID") or None, "error": f"{type(error).__name__}: {error}"})
        raise


if __name__ == "__main__":
    main()
