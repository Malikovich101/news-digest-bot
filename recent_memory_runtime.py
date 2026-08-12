import digest as digest
from datetime import datetime, timedelta, timezone
import json
import os
import re


MAX_SEMANTIC_COVERAGE_GAP = timedelta(hours=2)
BASE_SEMANTIC_DEDUPLICATE = digest.semantic_deduplicate


def install(digest_module):
    global digest
    digest = digest_module
    digest.load_state = load_state
    digest.save_state = save_state
    digest.prune_recent_news = prune_recent_news
    digest.cross_run_semantic_deduplicate = cross_run_semantic_deduplicate
    digest.collect_posts = collect_posts
    digest._base_semantic_deduplicate = BASE_SEMANTIC_DEDUPLICATE
    digest.semantic_deduplicate = semantic_deduplicate_with_temporal_guard


def parse_datetime(value, fallback):
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return fallback


def utc_now():
    return datetime.now(timezone.utc)


def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def prune_recent_news(recent_news, now):
    cutoff = now - timedelta(hours=36)
    valid = []
    for item in recent_news or []:
        if not isinstance(item, dict):
            continue
        delivered_at = parse_datetime(item.get("delivered_at"), None)
        if delivered_at is None or delivered_at < cutoff:
            continue
        item_id = item.get("id")
        text = item.get("text")
        date = item.get("date")
        if not item_id or not text or not date:
            continue
        valid.append({
            "id": item_id,
            "date": date,
            "delivered_at": delivered_at.isoformat(),
            "text": normalize_text(text)[:500],
        })
    valid.sort(key=lambda item: item["delivered_at"], reverse=True)
    return valid


def load_state():
    state_file = "state.json"
    if not os.path.exists(state_file):
        return {
            "version": 5,
            "channels": {},
            "delivered_ids": [],
            "delivered_chunks": [],
            "recent_news": [],
        }
    with open(state_file, "r", encoding="utf-8") as file:
        state = json.load(file)
    return {
        "version": 5,
        "channels": state.get("channels", {}),
        "delivered_ids": list(state.get("delivered_ids", []))[-2000:],
        "delivered_chunks": list(state.get("delivered_chunks", []))[-2000:],
        "recent_news": prune_recent_news(state.get("recent_news", []), utc_now()),
        "legacy_last_run": state.get("last_run"),
    }


def save_state(state):
    state.pop("legacy_last_run", None)
    state["version"] = 5
    state["delivered_ids"] = list(dict.fromkeys(state.get("delivered_ids", [])))[-2000:]
    state["delivered_chunks"] = list(dict.fromkeys(state.get("delivered_chunks", [])))[-2000:]
    state["recent_news"] = prune_recent_news(state.get("recent_news", []), utc_now())
    temp_file = "state.json.tmp"
    with open(temp_file, "w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp_file, state_file := "state.json")


def comparison_tokens(text):
    normalized = normalize_text(text).lower()
    normalized = re.sub(r"app\s*store", "appstore", normalized)
    url_re = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
    word_re = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)
    common_words = {
        "это", "как", "что", "для", "или", "при", "после", "через", "также",
        "будет", "были", "была", "есть", "еще", "новый", "новая", "новости",
        "сообщил", "сообщили", "компания", "сегодня", "теперь", "который",
    }
    tokens = set()
    for word in word_re.findall(url_re.sub(" ", normalized)):
        if word in common_words:
            continue
        tokens.add(word[:5] if len(word) > 5 else word)
    return tokens


def recent_history_candidates(current_posts, history, candidates_per_post=8):
    history_scored = []
    for item in history:
        tokens = comparison_tokens(item.get("text", ""))
        if tokens:
            history_scored.append((item, tokens))
    selected = {}
    for post in current_posts:
        current_tokens = comparison_tokens(post.get("text", ""))
        if not current_tokens:
            continue
        ranked = []
        for item, history_tokens in history_scored:
            common = len(current_tokens & history_tokens)
            union = len(current_tokens | history_tokens)
            jaccard = common / union if union else 0.0
            score = common + jaccard
            ranked.append((score, common, item))
        ranked.sort(key=lambda row: (row[0], row[1], row[2].get("delivered_at", "")), reverse=True)
        for score, common, item in ranked[:candidates_per_post]:
            if common >= 1:
                selected[item["id"]] = item
    return sorted(selected.values(), key=lambda item: item.get("delivered_at", ""), reverse=True)


def filter_posts_after_last_check(posts, state, replay_hours=0):
    if replay_hours:
        return posts, 0
    previous_channels = state.get("channels", {})
    filtered = []
    suppressed = 0
    for post in posts:
        channel_state = previous_channels.get(post.get("channel"), {})
        checked_at = parse_datetime(channel_state.get("last_checked_at"), None)
        post_date = parse_datetime(post.get("date"), None)
        if checked_at is not None and post_date is not None and post_date <= checked_at:
            suppressed += 1
            continue
        filtered.append(post)
    return filtered, suppressed


def collect_posts(client, channels, state, now, replay_hours=0):
    original_collect = getattr(collect_posts, "_original", None)
    if original_collect is None:
        original_collect = digest._base_collect_posts if hasattr(digest, "_base_collect_posts") else digest.collect_posts
    posts, next_state, failed_channels = original_collect(
        client, channels, state, now, replay_hours=replay_hours
    )
    filtered, suppressed = filter_posts_after_last_check(posts, state, replay_hours=replay_hours)
    if suppressed:
        print(
            f"Digest time window: suppressed {suppressed} messages at or before "
            "the previous successful channel check."
        )
    return filtered, next_state, failed_channels


# Capture the original collector before install() replaces it.
digest._base_collect_posts = digest.collect_posts
collect_posts._original = digest._base_collect_posts


def cross_run_semantic_deduplicate(client, posts, recent_news):
    if not posts or not recent_news:
        return posts, 0
    history = sorted(recent_news, key=lambda item: item.get("delivered_at", ""), reverse=True)
    dropped = set()
    for current_batch in digest.make_ai_batches(posts, digest.MAX_PREVIEW_CHARS):
        history_candidates = recent_history_candidates(current_batch, history)
        print(
            f"Cross-run semantic memory: {len(history)} history items, "
            f"{len(history_candidates)} candidates for current batch."
        )
        if not history_candidates:
            continue
        for start in range(0, len(history_candidates), 40):
            history_batch = history_candidates[start:start + 40]
            response = digest.generate_json(
                client,
                digest.recent_news_prompt(current_batch, history_batch),
            )
            dropped.update(
                digest.recent_news_repeat_ids(
                    response,
                    {post["id"] for post in current_batch},
                )
            )
    return [post for post in posts if post["id"] not in dropped], len(dropped)


def _restore_temporal_coverage(posts, kept_posts):
    if not posts:
        return kept_posts
    ordered = sorted(posts, key=lambda post: post.get("date", ""))
    kept_ids = {post["id"] for post in kept_posts}
    if not kept_ids:
        return ordered

    def dt(post):
        return parse_datetime(post.get("date"), None)

    restored = set(kept_ids)
    while True:
        current = [post for post in ordered if post["id"] in restored]
        dropped = [post for post in ordered if post["id"] not in restored and dt(post) is not None]
        best_gap = None
        best_candidate = None

        first_kept = dt(current[0])
        if first_kept is not None:
            earlier = [post for post in dropped if dt(post) < first_kept]
            if earlier:
                nearest = max(earlier, key=lambda post: dt(post))
                gap = first_kept - dt(nearest)
                if gap > MAX_SEMANTIC_COVERAGE_GAP:
                    best_gap = gap
                    best_candidate = nearest

        last_kept = dt(current[-1])
        if last_kept is not None:
            later = [post for post in dropped if dt(post) > last_kept]
            if later:
                nearest = min(later, key=lambda post: dt(post))
                gap = dt(nearest) - last_kept
                if gap > (best_gap or MAX_SEMANTIC_COVERAGE_GAP):
                    best_gap = gap
                    best_candidate = nearest

        for left, right in zip(current, current[1:]):
            left_dt = dt(left)
            right_dt = dt(right)
            if left_dt is None or right_dt is None:
                continue
            gap = right_dt - left_dt
            if gap <= MAX_SEMANTIC_COVERAGE_GAP:
                continue
            between = [post for post in dropped if left_dt < dt(post) < right_dt]
            if between and (best_gap is None or gap > best_gap):
                best_gap = gap
                best_candidate = min(between, key=lambda post: dt(post))

        if best_candidate is None:
            break
        restored.add(best_candidate["id"])

    return [post for post in ordered if post["id"] in restored]


def semantic_deduplicate_with_temporal_guard(client, posts):
    original = getattr(digest, "_base_semantic_deduplicate", BASE_SEMANTIC_DEDUPLICATE)
    kept, dropped = original(client, posts)
    protected = _restore_temporal_coverage(posts, kept)
    restored = len(protected) - len(kept)
    if restored:
        print(
            f"Semantic deduplication restored {restored} posts to preserve "
            f"time coverage (max gap {MAX_SEMANTIC_COVERAGE_GAP})."
        )
    return protected, max(0, dropped - restored)
