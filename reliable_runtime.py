import os

from google import genai
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from digest_pipeline import (
    DigestPipeline,
    cross_run_semantic_deduplicate,
    filter_and_deduplicate,
    format_post_time,
    load_channels,
    require_environment,
    replay_hours_from_environment,
    review_suspicious_ads,
    semantic_deduplicate,
    utc_now,
    watchdog_check,
    chunk_checkpoint_id,
    telegram_chunks,
)


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


def run_reliable_digest():
    """Run the production pipeline with at-least-once processing and idempotent delivery."""
    require_environment()
    now = utc_now()
    pipeline = DigestPipeline()
    state = pipeline.load_state()
    watchdog_missed = watchdog_check(state, now)
    channels = load_channels()
    replay_hours = replay_hours_from_environment()
    if not channels:
        raise RuntimeError("channels.txt is empty")

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
    if watchdog_missed:
        warnings.append("⚠️ Внимание: предыдущий дайджест был пропущен.")
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

    pipeline.send_telegram(
        os.environ["TG_BOT_TOKEN"],
        os.environ["TG_CHAT_ID"],
        "",
        state,
        posts,
        rendered_chunks=records,
    )

    if replay_hours == 0:
        delivered_at = utc_now()
        from digest_pipeline import remember_delivered_news
        remember_delivered_news(state, posts, delivered_at)
        state["last_successful_run"] = delivered_at.isoformat()
    pipeline.save_state(state)

    print(
        f"Delivered {len(posts)} canonical news posts from {len(collected)} collected "
        f"(semantic_duplicates={semantic_duplicates}, cross_run_duplicates={cross_run_duplicates}, "
        f"confirmed_ads={confirmed_ads}, replay_hours={replay_hours}, channels_updated={len(channel_updates)})"
    )


if __name__ == "__main__":
    run_reliable_digest()
