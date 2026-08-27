import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from google import genai
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from digest_pipeline import (
    CHUNK_DELAY_SECONDS,
    MAX_DELIVERED_CHUNKS,
    MAX_DELIVERED_IDS,
    PERM_TIMEZONE,
    RETRY_ATTEMPTS,
    DigestPipeline,
    chunk_checkpoint_id,
    cross_run_semantic_deduplicate,
    filter_and_deduplicate,
    format_post_time,
    load_channels,
    parse_datetime,
    replay_hours_from_environment,
    require_environment,
    review_suspicious_ads,
    semantic_deduplicate,
    telegram_chunks,
    utc_now,
)

DIGEST_WINDOWS = ((8, 5, "morning"), (14, 5, "afternoon"), (20, 5, "evening"))
DIGEST_STATE_KEY = "__digest__"
SCHEDULE_WARNING_MINUTES = 90


def _build_header(stats, semantic_duplicates, confirmed_ads, cross_run_duplicates, ai_unavailable):
    lines = [
        "❗❗❗❗❗❗",
        "🗞 Оригинальные новости",
        (
            f"📊 Постов с текстом: {stats['source_posts']}; явной рекламы: {stats['ads']}; "
            f"проверено Gemini: {stats.get('ad_review', 0)}; рекламы подтверждено Gemini: {confirmed_ads}; "
            f"отсеяно коротких: {stats['short']}; точных повторов: {stats['python_duplicates']}; "
            f"смысловых повторов: {semantic_duplicates}; повторов из прошлых дайджестов: {cross_run_duplicates}; "
            f"оригинальных публикаций: {len(stats.get('final_posts', []))}"
        ),
        "Каждый текст ниже — исходная публикация канала без пересказа и сокращения.",
    ]
    if ai_unavailable:
        lines.append("⚠️ Gemini недоступен: сомнительная реклама и semantic dedup пропущены, чтобы не потерять новости.")
    return "\n".join(lines)


def _post_block(post):
    return "\n".join([
        "────────────",
        f"🕒 {format_post_time(post['date'])} · {post['channel']}",
        post["text"],
        f"Источник: {post['url']}",
    ])


def build_delivery_chunks(posts, stats, semantic_duplicates, confirmed_ads, cross_run_duplicates, ai_unavailable=False, warnings=None):
    """Build Telegram chunks together with explicit post ownership."""
    stats = dict(stats)
    stats["final_posts"] = posts
    header = _build_header(stats, semantic_duplicates, confirmed_ads, cross_run_duplicates, ai_unavailable)
    records = [{"id": chunk_checkpoint_id(header), "text": header, "post_ids": []}]

    for post in posts:
        for part in telegram_chunks(_post_block(post)):
            records.append({"id": chunk_checkpoint_id(part), "text": part, "post_ids": [post["id"]]})

    for warning in warnings or []:
        records.append({"id": chunk_checkpoint_id(warning), "text": warning, "post_ids": []})

    return records


def send_reliable_chunks(token, chat_id, state, records, pipeline):
    """Deliver chunks idempotently; keep a post pending until every owned Telegram chunk is sent."""
    receipts = state.setdefault("delivery_receipts", {})
    completed = set(state.get("delivered_chunks", [])) | set(receipts)
    post_checkpoints = defaultdict(set)
    for record in records:
        for post_id in record.get("post_ids", []):
            post_checkpoints[post_id].add(record["id"])

    for index, record in enumerate(records):
        checkpoint = record["id"]
        if checkpoint in completed:
            continue

        last_error = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat_id, "text": record["text"]},
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    raise requests.RequestException(payload.get("description", "Telegram API rejected the message"))
                time.sleep(CHUNK_DELAY_SECONDS)
                break
            except requests.RequestException as error:
                last_error = error
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"Telegram delivery failed on chunk {index + 1}/{len(records)}: {last_error}")

        receipts[checkpoint] = {"sent_at": utc_now().isoformat(), "post_ids": record.get("post_ids", [])}
        state["delivered_chunks"] = list(dict.fromkeys(state.get("delivered_chunks", []) + [checkpoint]))[-MAX_DELIVERED_CHUNKS:]
        completed.add(checkpoint)

        for post_id in record.get("post_ids", []):
            required = post_checkpoints[post_id]
            if required.issubset(completed):
                state.setdefault("pending_posts", {}).pop(post_id, None)
                state.setdefault("delivered_ids", []).append(post_id)

        state["delivered_ids"] = list(dict.fromkeys(state.get("delivered_ids", [])))[-MAX_DELIVERED_IDS:]
        pipeline.save_state(state)

    pipeline.save_state(state)


def _window_at(local_date, hour, minute):
    return datetime.combine(local_date, datetime.min.time(), tzinfo=PERM_TIMEZONE).replace(hour=hour, minute=minute)


def latest_due_window(now):
    """Return the latest logical digest window that has started today, or None before 08:05."""
    local_now = now.astimezone(PERM_TIMEZONE)
    latest = None
    for hour, minute, _name in DIGEST_WINDOWS:
        candidate = _window_at(local_now.date(), hour, minute)
        if candidate <= local_now:
            latest = candidate
    return latest


def next_window_after(window):
    """Return the first logical digest window after the supplied window."""
    local_window = window.astimezone(PERM_TIMEZONE)
    for hour, minute, _name in DIGEST_WINDOWS:
        candidate = _window_at(local_window.date(), hour, minute)
        if candidate > local_window:
            return candidate
    next_date = local_window.date() + timedelta(days=1)
    hour, minute, _name = DIGEST_WINDOWS[0]
    return _window_at(next_date, hour, minute)


def _last_successful_window(state):
    digest_state = state.get("channels", {}).get(DIGEST_STATE_KEY, {})
    if not isinstance(digest_state, dict):
        return None
    return parse_datetime(digest_state.get("last_successful_window"), None)


def _set_last_successful_window(state, window):
    channels = state.setdefault("channels", {})
    digest_state = channels.setdefault(DIGEST_STATE_KEY, {})
    digest_state["last_successful_window"] = window.isoformat()


def scheduled_window_due(state, now):
    """Return whether a new logical digest window is due and the latest eligible window."""
    due_window = latest_due_window(now)
    if due_window is None:
        return False, None
    last_window = _last_successful_window(state)
    return last_window is None or due_window > last_window, due_window


def schedule_health_warning(state, now):
    """Warn only when the first missed logical window is more than 90 minutes late."""
    due_window = latest_due_window(now)
    last_window = _last_successful_window(state)
    if due_window is None or last_window is None or due_window <= last_window:
        return None
    first_missed = next_window_after(last_window)
    if first_missed > due_window:
        return None
    delay_minutes = int((now - first_missed.astimezone(now.tzinfo)).total_seconds() // 60)
    if delay_minutes <= SCHEDULE_WARNING_MINUTES:
        return None
    return (
        f"⚠️ Внимание: предыдущий дайджест был задержан на {delay_minutes} мин. "
        f"Автоматически выполнено восстановление пропущенного окна."
    )


def run_reliable_digest():
    """Run the production pipeline with logical-window catch-up and idempotent delivery."""
    require_environment()
    now = utc_now()
    pipeline = DigestPipeline()
    state = pipeline.load_state()
    channels = load_channels()
    replay_hours = replay_hours_from_environment()
    if not channels:
        raise RuntimeError("channels.txt is empty")

    scheduled_run = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    due, due_window = scheduled_window_due(state, now)
    if scheduled_run and replay_hours == 0 and not due:
        print("No digest window is due; scheduled check exits without collecting or sending news.")
        return

    schedule_warning = schedule_health_warning(state, now) if replay_hours == 0 else None

    pending = [item for item in state.get("pending_posts", {}).values() if isinstance(item, dict)]
    if replay_hours == 0 and pending:
        collected = pending
        channel_updates = {}
        failed_channels = []
    else:
        with TelegramClient(
            StringSession(os.environ["TG_SESSION_STRING"]),
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
        ) as telegram_client:
            collected, channel_updates, failed_channels = pipeline.collect_posts(
                telegram_client, channels, state, now, replay_hours
            )
        if failed_channels and len(failed_channels) == len(channels):
            raise RuntimeError("All configured channels failed to load")

    collected.sort(key=lambda post: post["date"])
    deterministic_posts, stats = filter_and_deduplicate(collected)
    state["channels"].update(channel_updates)
    if replay_hours == 0 and deterministic_posts:
        pipeline.add_pending_posts(state, deterministic_posts, now)
    pipeline.save_state(state)

    candidates = list(deterministic_posts)
    posts = candidates
    ai_unavailable = False
    confirmed_ads = semantic_duplicates = cross_run_duplicates = 0

    if candidates:
        try:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            posts, confirmed_ads = review_suspicious_ads(client, candidates)
            if replay_hours == 0:
                by_id = {}
                for item in state.get("recent_news", []) + state.get("event_memory", []):
                    if isinstance(item, dict) and item.get("id"):
                        by_id[item["id"]] = item
                posts, cross_run_duplicates = cross_run_semantic_deduplicate(client, posts, list(by_id.values()))
            posts, semantic_duplicates = semantic_deduplicate(client, posts)
        except RuntimeError as error:
            ai_unavailable = True
            posts = candidates
            print(f"Gemini unavailable, using deterministic candidates: {error}")

    collected_ids = {post["id"] for post in collected}
    final_ids = {post["id"] for post in posts}
    for post_id in collected_ids - final_ids:
        state.get("pending_posts", {}).pop(post_id, None)
    pipeline.save_state(state)

    warnings = []
    if ai_unavailable:
        warnings.append("⚠️ Gemini недоступен: возможны дубли.")
    if schedule_warning:
        warnings.append(schedule_warning)
    if failed_channels:
        warnings.append("⚠️ Не удалось проверить: " + ", ".join(failed_channels))

    if posts:
        records = build_delivery_chunks(
            posts,
            stats,
            semantic_duplicates,
            confirmed_ads,
            cross_run_duplicates,
            ai_unavailable=ai_unavailable,
            warnings=warnings,
        )
    else:
        empty_text = "❗❗❗❗❗❗\n🗞 За этот период новых подходящих новостей не было."
        records = [{"id": chunk_checkpoint_id(empty_text), "text": empty_text, "post_ids": []}]
        records.extend({"id": chunk_checkpoint_id(w), "text": w, "post_ids": []} for w in warnings)

    send_reliable_chunks(os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"], state, records, pipeline)

    if replay_hours == 0:
        delivered_at = utc_now()
        from digest_pipeline import remember_delivered_news
        remember_delivered_news(state, posts, delivered_at)
        state["last_successful_run"] = delivered_at.isoformat()
        if due_window is not None:
            _set_last_successful_window(state, due_window)
    pipeline.save_state(state)

    print(
        f"Delivered {len(posts)} canonical news posts from {len(collected)} collected "
        f"(semantic_duplicates={semantic_duplicates}, cross_run_duplicates={cross_run_duplicates}, "
        f"confirmed_ads={confirmed_ads}, replay_hours={replay_hours}, "
        f"channels_updated={len(channel_updates)}, due_window={due_window.isoformat() if due_window else 'manual'})"
    )


if __name__ == "__main__":
    run_reliable_digest()
