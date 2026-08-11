# Digest window regression

The production digest must not include Telegram posts whose publication time is at or before the previous successful per-channel check. This protects against Telegram history/backfill behavior returning older messages even when message IDs are expected to advance monotonically.
