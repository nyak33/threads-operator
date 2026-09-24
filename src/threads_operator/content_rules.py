"""Central Threads content constraints enforced by Threads Operator.

These constants describe capabilities that this repo actually publishes through
the standard Threads TEXT API path. UI-only or separately implemented Threads
features must not silently relax these limits.

Platform snapshot last verified against official Meta sources: 2026-09-24.
See docs/threads-platform-rules.md for sources and maintenance policy.
"""
from __future__ import annotations

STANDARD_POST_MAX_CHARS = 500
TEXT_ATTACHMENT_MAX_CHARS = 10_000
RULES_LAST_VERIFIED = "2026-09-24"
RULES_REVERIFY_AFTER_DAYS = 30


def validate_standard_post_text(text: str, *, label: str = "Threads text") -> None:
    """Validate one standard Threads root/reply text before it reaches Meta."""
    if not isinstance(text, str):
        raise ValueError(f"{label} must be a string")
    if not text.strip():
        raise ValueError(f"{label} must not be empty")

    length = len(text)
    if length > STANDARD_POST_MAX_CHARS:
        raise ValueError(
            f"{label} exceeds the standard Threads post limit: "
            f"{length}/{STANDARD_POST_MAX_CHARS} characters. "
            "Threads Operator currently publishes standard TEXT posts/replies; "
            "the separate long-text attachment feature is not enabled here."
        )


def validate_thread_texts(main_post_text: str, reply_texts: list[str]) -> None:
    """Validate the entire chain before any part of it is published."""
    validate_standard_post_text(main_post_text, label="main post")
    for index, reply in enumerate(reply_texts, start=1):
        validate_standard_post_text(reply, label=f"reply {index}")
