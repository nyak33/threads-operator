"""Topic handling for queue rows, centralized.

Every content queue stores its topic under a table-specific column. This
module maps each known queue table to its column and normalizes the raw
value into a Meta-safe ``topic_tag`` so no publish path has to know the
per-table schema or Meta's constraints.

Meta Threads API ``topic_tag`` rules (create-container request):
- 1..50 characters after trimming
- no periods (.), ampersands (&), at signs (@), exclamation/question marks,
  commas, semi-colons, colons, or leading hash signs
- one topic per post; it belongs on the root post, not replies

Rows that predate topics, or whose topic column is NULL/empty, resolve to
None and publish exactly as before — a missing topic must never crash
publishing.
"""

from __future__ import annotations

import re
from typing import Any

# Queue table -> topic column. Keep in sync with the live Supabase schema:
#   threads_publish_queue.topic
#   threads_content_queue.topic_tag
#   note_to_self_queue.topic_tag
#   affiliate_queue.topic
#   hadith_content_queue.topic
TOPIC_FIELDS: dict[str, str] = {
    "threads_publish_queue": "topic",
    "threads_content_queue": "topic_tag",
    "note_to_self_queue": "topic_tag",
    "affiliate_queue": "topic",
    "hadith_content_queue": "topic",
}

# Fallback column order for tables not in the explicit map (new/legacy
# queues). First non-empty match wins.
_FALLBACK_FIELDS = ("topic_tag", "topic")

# Characters Meta rejects inside topic_tag. Spaces ARE allowed by Meta.
_INVALID_CHARS = re.compile(r"[.&@!?,;:#]")
_MAX_LEN = 50


def normalize_topic(raw: Any) -> str | None:
    """Return a Meta-safe topic tag, or None when there is nothing usable.

    Never raises: any non-string/empty/over-long-after-cleanup value simply
    becomes None so publishing proceeds untagged rather than crashing.
    """
    if not isinstance(raw, str):
        return None
    topic = _INVALID_CHARS.sub("", raw).strip()
    # Collapse internal whitespace runs so tags stay tidy.
    topic = re.sub(r"\s+", " ", topic)
    if not topic:
        return None
    if len(topic) > _MAX_LEN:
        topic = topic[:_MAX_LEN].rstrip()
    return topic or None


def topic_field_for_table(table: str) -> str | None:
    """The topic column for a known queue table, else None (use fallback)."""
    return TOPIC_FIELDS.get(table)


def resolve_topic(table: str, row: dict[str, Any] | None) -> str | None:
    """Extract and normalize the topic from a claimed queue row.

    Uses the explicit per-table column first; for unknown tables, probes the
    fallback columns. Always returns a Meta-safe value or None.
    """
    if not row:
        return None
    field = TOPIC_FIELDS.get(table)
    if field is not None:
        return normalize_topic(row.get(field))
    for candidate in _FALLBACK_FIELDS:
        topic = normalize_topic(row.get(candidate))
        if topic is not None:
            return topic
    return None
