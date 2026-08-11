from datetime import datetime, timedelta, timezone
import json
import os
import re


def install(digest):
    digest.load_state = load_state
    digest.save_state = save_state
    digest.prune_recent_news = prune_recent_news
    digest.cross_run_semantic_deduplicate = cross_run_semantic_deduplicate


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
