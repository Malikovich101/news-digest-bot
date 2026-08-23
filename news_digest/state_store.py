import json
import os
from datetime import timedelta

from digest_pipeline import utc_now, parse_datetime

STATE_VERSION = 6
PENDING_TTL_HOURS = 96
MAX_PENDING_POSTS = 5000
MAX_DELIVERED_CHUNKS = 4000
MAX_DELIVERED_IDS = 5000
MAX_EVENT_MEMORY = 1000


def empty_state():
    return {
        "version": STATE_VERSION,
        "channels": {},
        "pending_posts": {},
        "delivered_ids": [],
        "delivery_receipts": {},
        "recent_news": [],
        "event_memory": [],
        "last_successful_run": None,
    }


def _dedupe_list(values, limit):
    return list(dict.fromkeys(values or []))[-limit:]


def migrate_state(raw):
    raw = raw if isinstance(raw, dict) else {}
    state = empty_state()
    state["channels"] = raw.get("channels", {})
    state["pending_posts"] = raw.get("pending_posts", {}) or {}
    state["delivered_ids"] = _dedupe_list(raw.get("delivered_ids", []), MAX_DELIVERED_IDS)
    state["delivery_receipts"] = raw.get("delivery_receipts", {}) or {}
    state["recent_news"] = raw.get("recent_news", []) or []
    state["event_memory"] = raw.get("event_memory", []) or []
    state["last_successful_run"] = raw.get("last_successful_run")

    # Migrate the old long-term memory into event_memory without inventing semantics.
    if not state["event_memory"] and state["recent_news"]:
        for item in state["recent_news"]:
            if not isinstance(item, dict):
                continue
            state["event_memory"].append({
                "id": item.get("id"),
                "date": item.get("date"),
                "delivered_at": item.get("delivered_at"),
                "text": item.get("text", ""),
                "event_fingerprint": item.get("event_fingerprint"),
            })

    state["version"] = STATE_VERSION
    return state


def load_state(path):
    if not os.path.exists(path):
        return empty_state()
    with open(path, "r", encoding="utf-8") as file:
        return migrate_state(json.load(file))


def prune_pending(state, now):
    cutoff = now - timedelta(hours=PENDING_TTL_HOURS)
    pending = {}
    for post_id, post in state.get("pending_posts", {}).items():
        if not isinstance(post, dict):
            continue
        collected_at = parse_datetime(post.get("collected_at"), None)
        if collected_at is None or collected_at >= cutoff:
            pending[post_id] = post
    state["pending_posts"] = dict(list(pending.items())[-MAX_PENDING_POSTS:])


def normalize_state(state):
    now = utc_now()
    state = migrate_state(state)
    prune_pending(state, now)
    state["delivered_ids"] = _dedupe_list(state.get("delivered_ids", []), MAX_DELIVERED_IDS)
    state["delivery_receipts"] = dict(list(state.get("delivery_receipts", {}).items())[-MAX_DELIVERED_CHUNKS:])
    state["event_memory"] = list(state.get("event_memory", []))[-MAX_EVENT_MEMORY:]
    state["version"] = STATE_VERSION
    return state


def save_state(path, state):
    state = normalize_state(state)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp_path, path)
