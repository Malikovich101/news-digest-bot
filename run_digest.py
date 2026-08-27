"""Production entrypoint with an additional quality-control layer.

The reliability/runtime code remains unchanged. This module patches the
pipeline functions before importing reliable_runtime so content quality is
hardened without touching delivery/state machinery.
"""

import digest_pipeline as dp


# High-confidence markers that should never reach the digest. Everything else
# is reviewed by Gemini; this avoids false positives from ordinary news that
# happens to mention prices, purchases or discounts.
STRONG_AD_MARKERS = (
    "#реклама",
    "#рекламa",
    "реклама:",
    "erid:",
    "erid ",
    "рекламная интеграция",
    "на правах рекламы",
    "партнёрский материал",
    "партнерский материал",
    "спецпроект",
    "нативная реклама",
    "рекламодатель",
    "коммерческое предложение",
    "промокод",
    "промо-код",
)


def _obvious_ad(text):
    normalized = dp.analysis_text(text).lower()
    return any(marker in normalized for marker in STRONG_AD_MARKERS)


def review_all_ads(client, posts):
    """Review every non-obvious candidate, not only promo-looking posts."""
    if not posts:
        return posts, 0

    obvious = {post["id"] for post in posts if _obvious_ad(post["text"])}
    review = [post for post in posts if post["id"] not in obvious]
    dropped = set(obvious)

    # The old implementation sent only posts containing a small set of promo
    # words to Gemini. Native/advertorial posts without those words escaped.
    for batch in dp.make_ai_batches(review, dp.MAX_MODEL_POST_CHARS):
        response = dp.generate_json(client, dp.ad_review_prompt(batch))
        dropped.update(
            dp.ad_ids_from_response(response, {post["id"] for post in batch})
        )

    kept = [post for post in posts if post["id"] not in dropped]
    return kept, len(dropped)


def enhanced_semantic_deduplicate(client, posts):
    """Deduplicate globally, with overlapping AI windows plus focused passes."""
    if len(posts) < 2:
        return posts, 0

    dropped = set()

    # Compare the whole stream, not only deterministic token clusters.
    # Overlap prevents duplicates near an AI-batch boundary from escaping.
    for batch in dp.make_ai_batches(posts, dp.MAX_PREVIEW_CHARS, overlap=30):
        active = [post for post in batch if post["id"] not in dropped]
        if len(active) < 2:
            continue
        response = dp.generate_json(
            client, dp.duplicate_prompt(active, dp.MAX_PREVIEW_CHARS)
        )
        dropped.update(
            dp.duplicate_ids_from_response(
                response, {post["id"] for post in active}
            )
        )

    # Re-check lexical clusters with the full post text.
    remaining = [post for post in posts if post["id"] not in dropped]
    for cluster in dp.candidate_clusters(remaining):
        active = [post for post in cluster if post["id"] not in dropped]
        if len(active) < 2:
            continue
        response = dp.generate_json(
            client, dp.duplicate_prompt(active, dp.MAX_MODEL_POST_CHARS)
        )
        dropped.update(
            dp.duplicate_ids_from_response(
                response, {post["id"] for post in active}
            )
        )

    return [post for post in posts if post["id"] not in dropped], len(dropped)


def enhanced_cross_run_deduplicate(client, posts, recent_news):
    """Cross-run dedup with overlapping current-news windows."""
    if not posts or not recent_news:
        return posts, 0

    history = sorted(
        recent_news,
        key=lambda item: item.get("delivered_at", ""),
        reverse=True,
    )
    dropped = set()
    for current_batch in dp.make_ai_batches(posts, dp.MAX_PREVIEW_CHARS, overlap=20):
        active = [post for post in current_batch if post["id"] not in dropped]
        if not active:
            continue
        for start in range(0, len(history), dp.RECENT_NEWS_HISTORY_BATCH):
            history_batch = history[start:start + dp.RECENT_NEWS_HISTORY_BATCH]
            response = dp.generate_json(
                client, dp.recent_news_prompt(active, history_batch)
            )
            dropped.update(
                dp.recent_news_repeat_ids(
                    response, {post["id"] for post in active}
                )
            )
    return [post for post in posts if post["id"] not in dropped], len(dropped)


# Patch before reliable_runtime imports these symbols with ``from ... import``.
dp.review_suspicious_ads = review_all_ads
dp.semantic_deduplicate = enhanced_semantic_deduplicate
dp.cross_run_semantic_deduplicate = enhanced_cross_run_deduplicate

from reliable_runtime import run_reliable_digest


if __name__ == "__main__":
    run_reliable_digest()
