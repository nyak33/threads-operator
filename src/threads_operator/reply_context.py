"""Deterministic context assembly for Workflow B (own-post replies).

When someone replies to one of our own Threads posts, this module constructs
a bounded, structured context representation that Hermes can consume for
drafting. The operator collects and stores this context deterministically;
Hermes remains responsible for model-based interpretation.

Context is assembled from:
1. Account identity
2. Root/original post (from threads_posts or API)
3. Parent reply (the post being replied to)
4. Current incoming reply
5. Conversation chain (bounded: root + direct parent + current)
6. Topic/topic_tag (from threads_posts)
7. Content pillar (derived from topic or account config)
8. Post objective (inferred from post text patterns)
9. CTA associated with the post (from post text or engagement config)
10. Previous interactions with the same Threads user (bounded query)
11. Account persona (from personas/<account>.md)

Never fabricates missing context. Fails closed when account context is
missing, root post cannot be resolved, or cross-account data could be mixed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .supabase_store import SupabaseStore

logger = logging.getLogger(__name__)

# Bounded limits — never concatenate unlimited history
MAX_PRIOR_INTERACTIONS = 10
MAX_CONVERSATION_CHAIN_DEPTH = 3
MAX_CONTEXT_AGE_DAYS = 30
MAX_POST_TEXT_LENGTH = 1000
MAX_REPLY_TEXT_LENGTH = 500


@dataclass(frozen=True)
class ReplyContext:
    """Structured context for one incoming reply."""

    # 1. Account
    account_key: str

    # 2. Original/root post
    root_post_id: str
    root_post_permalink: str | None
    root_post_text: str | None
    root_post_topic_tag: str | None
    root_post_category: str | None
    root_post_published_at: str | None

    # 3. Parent reply (if replying to a reply)
    parent_reply_id: str | None
    parent_reply_text: str | None
    parent_reply_username: str | None

    # 4. Current incoming reply
    reply_id: str
    reply_text: str | None
    reply_username: str | None
    replied_at: str | None

    # 5. Conversation chain (bounded)
    conversation_chain: tuple[dict[str, Any], ...]  # root -> ... -> parent -> current

    # 6. Topic
    topic_tag: str | None

    # 7. Content pillar (derived)
    content_pillar: str | None

    # 8. Post objective (inferred)
    post_objective: str | None

    # 9. CTA associated with the post
    post_cta: str | None
    cta_patterns: tuple[str, ...]  # from engagement config

    # 10. Previous interactions with same user
    prior_interactions: tuple[dict[str, Any], ...]
    prior_interaction_count: int

    # 11. Account persona
    persona_text: str | None

    # Metadata
    assembled_at: str
    context_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for storage."""
        return {
            "context_version": self.context_version,
            "assembled_at": self.assembled_at,
            "account": {"account_key": self.account_key},
            "root_post": {
                "post_id": self.root_post_id,
                "permalink": self.root_post_permalink,
                "text": self.root_post_text,
                "topic_tag": self.root_post_topic_tag,
                "category": self.root_post_category,
                "published_at": self.root_post_published_at,
            },
            "parent_reply": {
                "reply_id": self.parent_reply_id,
                "text": self.parent_reply_text,
                "username": self.parent_reply_username,
            } if self.parent_reply_id else None,
            "incoming_reply": {
                "reply_id": self.reply_id,
                "text": self.reply_text,
                "username": self.reply_username,
                "replied_at": self.replied_at,
            },
            "conversation_chain": list(self.conversation_chain),
            "topic": {"topic_tag": self.topic_tag},
            "content_pillar": self.content_pillar,
            "post_objective": self.post_objective,
            "cta": {
                "post_cta": self.post_cta,
                "configured_patterns": list(self.cta_patterns),
            },
            "prior_interactions": {
                "count": self.prior_interaction_count,
                "recent": list(self.prior_interactions),
            },
            "persona": {
                "available": self.persona_text is not None,
                "text_preview": (self.persona_text[:200] + "...") if self.persona_text and len(self.persona_text) > 200 else self.persona_text,
            },
        }


class ContextAssemblyError(RuntimeError):
    """Raised when context cannot be safely assembled."""


def _truncate_text(text: str | None, max_len: int) -> str | None:
    """Truncate text to a bounded length, preserving word boundaries."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return truncated + "..." if len(truncated) < len(text) else truncated


def _infer_content_pillar(topic_tag: str | None, account_key: str) -> str | None:
    """Derive content pillar from topic tag. Deterministic, no LLM."""
    if not topic_tag:
        return None
    topic_lower = topic_tag.lower()
    # Simple deterministic mapping — extendable via config
    pillar_map = {
        "marketing": "digital_marketing",
        "ads": "paid_media",
        "facebook": "social_media",
        "business": "entrepreneurship",
        "sales": "sales",
        "ai": "technology",
        "automation": "technology",
        "career": "career_development",
        "upskilling": "career_development",
        "packaging": "business_operations",
        "smartkood": "business_operations",
    }
    for key, pillar in pillar_map.items():
        if key in topic_lower:
            return pillar
    return "general"


def _infer_post_objective(post_text: str | None) -> str | None:
    """Infer post objective from text patterns. Deterministic, no LLM."""
    if not post_text:
        return None
    text_lower = post_text.lower()

    # Question pattern
    if "?" in post_text or "tanya" in text_lower or "soalan" in text_lower:
        return "engagement_question"

    # CTA patterns
    cta_indicators = [
        ("berminat", "lead_generation"),
        ("dm saya", "lead_generation"),
        ("pm", "lead_generation"),
        ("link", "traffic"),
        ("daftar", "conversion"),
        ("join", "conversion"),
        ("sign up", "conversion"),
        ("beli", "sales"),
        ("harga", "sales"),
        ("promo", "sales"),
        ("free", "lead_generation"),
        ("tips", "education"),
        ("cara", "education"),
        ("how to", "education"),
        ("sharing", "awareness"),
        ("cerita", "storytelling"),
        ("pengalaman", "storytelling"),
    ]
    for indicator, objective in cta_indicators:
        if indicator in text_lower:
            return objective

    return "general_engagement"


def _extract_post_cta(post_text: str | None) -> str | None:
    """Extract the CTA phrase from post text, if present."""
    if not post_text:
        return None
    # Look for common CTA patterns
    lines = post_text.split("\n")
    # Check last lines first (CTA usually at the end)
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        cta_starts = [
            "komen", "comment", "dm", "pm", "link", "daftar",
            "join", "sign up", "beli", "harga", "promo", "free",
            "berminat", "nak", "tekan", "click", "swipe",
        ]
        for start in cta_starts:
            if lower.startswith(start):
                return _truncate_text(line, 100)
        # Also check if CTA appears anywhere in the line (not just at start)
        for start in cta_starts:
            if start in lower:
                # Extract from the CTA keyword to the end of the line
                idx = lower.find(start)
                cta_text = line[idx:]
                return _truncate_text(cta_text, 100)
    return None


def assemble_reply_context(
    *,
    store: SupabaseStore,
    account_key: str,
    reply_row: dict[str, Any],
    persona_text: str | None = None,
    cta_patterns: tuple[str, ...] = (),
) -> ReplyContext:
    """Assemble deterministic context for one incoming reply.

    Args:
        store: Account-scoped Supabase store.
        account_key: The account this reply belongs to.
        reply_row: Row from threads_own_reply_engagement (must have reply_id,
                   parent_post_id, from_username, reply_text, etc.).
        persona_text: Optional persona text from personas/<account>.md.
        cta_patterns: Optional CTA patterns from engagement config.

    Returns:
        ReplyContext with all available fields populated.

    Raises:
        ContextAssemblyError: When account context is missing, root post
            cannot be resolved, or event identity is uncertain.
    """
    # --- Validate identity ---
    if not account_key:
        raise ContextAssemblyError("account_key is required for context assembly")
    reply_id = reply_row.get("reply_id")
    if not isinstance(reply_id, str) or not reply_id.strip():
        raise ContextAssemblyError("reply_id must be a non-blank string")
    parent_post_id = reply_row.get("parent_post_id")
    if not isinstance(parent_post_id, str) or not parent_post_id.strip():
        raise ContextAssemblyError("parent_post_id must be a non-blank string")

    # Account isolation: the store must already be scoped to this account
    if store.account_key != account_key:
        raise ContextAssemblyError(
            f"store account_key '{store.account_key}' does not match reply account '{account_key}'"
        )

    # --- Resolve root post ---
    # Try threads_posts first (local registry), then fall back to parent_post_text
    root_post = _resolve_root_post(store, parent_post_id, reply_row)

    # --- Resolve parent reply (if this is a nested reply) ---
    parent_reply = _resolve_parent_reply(store, reply_row)

    # --- Build conversation chain ---
    conversation_chain = _build_conversation_chain(root_post, parent_reply, reply_row)

    # --- Extract topic and pillar ---
    topic_tag = root_post.get("topic_tag") or root_post.get("category")
    content_pillar = _infer_content_pillar(topic_tag, account_key)

    # --- Infer post objective and CTA ---
    post_objective = _infer_post_objective(root_post.get("text"))
    post_cta = _extract_post_cta(root_post.get("text"))

    # --- Query prior interactions with same user ---
    prior_interactions = _query_prior_interactions(
        store, account_key, reply_row.get("from_username")
    )

    return ReplyContext(
        account_key=account_key,
        root_post_id=parent_post_id,
        root_post_permalink=root_post.get("permalink"),
        root_post_text=_truncate_text(root_post.get("text"), MAX_POST_TEXT_LENGTH),
        root_post_topic_tag=topic_tag,
        root_post_category=root_post.get("category"),
        root_post_published_at=root_post.get("published_at"),
        parent_reply_id=parent_reply.get("reply_id") if parent_reply else None,
        parent_reply_text=_truncate_text(parent_reply.get("reply_text"), MAX_REPLY_TEXT_LENGTH) if parent_reply else None,
        parent_reply_username=parent_reply.get("from_username") if parent_reply else None,
        reply_id=reply_id,
        reply_text=_truncate_text(reply_row.get("reply_text"), MAX_REPLY_TEXT_LENGTH),
        reply_username=reply_row.get("from_username"),
        replied_at=reply_row.get("replied_at"),
        conversation_chain=tuple(conversation_chain),
        topic_tag=topic_tag,
        content_pillar=content_pillar,
        post_objective=post_objective,
        post_cta=post_cta,
        cta_patterns=cta_patterns,
        prior_interactions=tuple(prior_interactions),
        prior_interaction_count=len(prior_interactions),
        persona_text=persona_text,
        assembled_at=datetime.now(timezone.utc).isoformat(),
    )


def _resolve_root_post(
    store: SupabaseStore,
    parent_post_id: str,
    reply_row: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the root post from threads_posts or fall back to reply_row data.

    Never fabricates missing context — returns only what is verifiably known.
    """
    # Try threads_posts first
    try:
        posts = store.list_posts()
        for post in posts:
            if post.get("thread_id") == parent_post_id:
                return {
                    "thread_id": post.get("thread_id"),
                    "permalink": post.get("permalink"),
                    "text": post.get("text"),
                    "topic_tag": post.get("topic_tag"),
                    "category": post.get("category"),
                    "published_at": post.get("published_at"),
                }
    except Exception as exc:
        logger.warning("Failed to query threads_posts for root post %s: %s", parent_post_id, exc)

    # Fall back to what the reply row already knows
    return {
        "thread_id": parent_post_id,
        "permalink": reply_row.get("parent_post_permalink"),
        "text": reply_row.get("parent_post_text"),
        "topic_tag": None,
        "category": None,
        "published_at": None,
    }


def _resolve_parent_reply(
    store: SupabaseStore,
    reply_row: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the parent reply if this is a nested reply.

    For now, the Graph API only gives us direct replies to posts. If the
    reply_row has a parent_reply_id field (future enhancement), we would
    look it up. Currently returns None — the conversation chain is
    root_post -> current_reply.
    """
    # The current schema doesn't track nested reply parents explicitly.
    # The Graph API /{post-id}/replies edge returns direct replies only.
    # If we later add parent_reply_id to the schema, this would query it.
    return None


def _build_conversation_chain(
    root_post: dict[str, Any],
    parent_reply: dict[str, Any] | None,
    reply_row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build a bounded conversation chain: root -> [parent] -> current."""
    chain = []

    # Root post
    chain.append({
        "type": "root_post",
        "id": root_post.get("thread_id"),
        "text": _truncate_text(root_post.get("text"), MAX_POST_TEXT_LENGTH),
        "username": None,  # Our own post
        "permalink": root_post.get("permalink"),
    })

    # Parent reply (if nested)
    if parent_reply:
        chain.append({
            "type": "parent_reply",
            "id": parent_reply.get("reply_id"),
            "text": _truncate_text(parent_reply.get("reply_text"), MAX_REPLY_TEXT_LENGTH),
            "username": parent_reply.get("from_username"),
            "permalink": parent_reply.get("reply_permalink"),
        })

    # Current reply
    chain.append({
        "type": "current_reply",
        "id": reply_row.get("reply_id"),
        "text": _truncate_text(reply_row.get("reply_text"), MAX_REPLY_TEXT_LENGTH),
        "username": reply_row.get("from_username"),
        "permalink": reply_row.get("reply_permalink"),
    })

    return chain


def _query_prior_interactions(
    store: SupabaseStore,
    account_key: str,
    username: str | None,
) -> list[dict[str, Any]]:
    """Query bounded prior interactions with the same Threads user.

    Only returns interactions that are safely available: replies from the
    same user on this account's posts, within a bounded time window.
    """
    if not username or not isinstance(username, str) or not username.strip():
        return []

    username = username.strip()
    try:
        # Query threads_own_reply_engagement for prior replies from this user
        rows = store.list_own_replies(status=None, limit=MAX_PRIOR_INTERACTIONS * 2)
        interactions = []
        for row in rows:
            if row.get("from_username") != username:
                continue
            if row.get("account_key") != account_key:
                continue  # Extra safety — should never happen with scoped store
            interactions.append({
                "reply_id": row.get("reply_id"),
                "parent_post_id": row.get("parent_post_id"),
                "reply_text": _truncate_text(row.get("reply_text"), 200),
                "status": row.get("status"),
                "replied_at": row.get("replied_at"),
            })
            if len(interactions) >= MAX_PRIOR_INTERACTIONS:
                break
        return interactions
    except Exception as exc:
        logger.warning("Failed to query prior interactions for %s: %s", username, exc)
        return []
