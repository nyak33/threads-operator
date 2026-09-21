"""Workflow B: replies under our OWN posts (approval-gated).

State machine on ``threads_own_reply_engagement`` (migration 010):

    discovered -> pending_approval -> approved -> posting -> posted
                                   -> rejected (terminal)
                                   -> ignored  (terminal)
                                   -> failed   (transient retry / terminal alert)

Discovery uses ONLY the official Threads Graph API (``/{post-id}/replies``
via ThreadsAPI.list_direct_replies). Publishing uses the proven
create-container -> threads_publish flow with ``reply_to_id`` set to the
inbound reply's Graph media id. Nothing in this module uses browser
automation, and nothing publishes without an explicit Telebot approval
transition (pending_approval -> approved) recorded in Supabase first.
"""

from __future__ import annotations

import logging
from typing import Any

from . import content_generation
from .trend_engagement import THREADS_CHAR_LIMIT

logger = logging.getLogger(__name__)

# Transient Graph API error substrings that justify a retry with backoff.
# Everything else (auth, permission, not-found) is permanent for the row.
_TRANSIENT_MARKERS = (
    "rate limit",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "500",
    "502",
    "503",
    "504",
    "connection",
)

MAX_REPLY_ATTEMPTS = 5


def is_transient_error(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _response_system_prompt(persona_text: str) -> str:
    return (
        "You draft ONE reply to a comment on the account's own Threads post.\n\n"
        f"ACCOUNT PERSONA:\n{persona_text}\n\n"
        "RULES:\n"
        "- Reply in the persona's voice and language (Malay-first for syaqir).\n"
        "- 1-3 short sentences. Casual, direct, no corporate tone.\n"
        "- Answer the commenter's actual point; thank them only if natural.\n"
        "- Never fabricate experience or facts the persona does not claim.\n"
        f"- HARD LIMIT: {THREADS_CHAR_LIMIT} characters.\n"
        "- Output ONLY the reply text. No preamble."
    )


def generate_reply_draft(
    *,
    persona_text: str,
    parent_post_text: str | None,
    from_username: str | None,
    reply_text: str | None,
    providers: list[content_generation.LLMProviderSpec] | None = None,
    client: Any = None,
) -> str:
    user_prompt = (
        f"Our original post:\n{(parent_post_text or '(unavailable)')[:600]}\n\n"
        f"Reply from @{from_username or 'someone'}:\n{(reply_text or '(text unavailable)')[:600]}\n\n"
        "Draft the response."
    )
    draft = content_generation.generate_text(
        system_prompt=_response_system_prompt(persona_text),
        user_prompt=user_prompt,
        max_output_tokens=400,
        providers=providers,
        client=client,
    )
    if len(draft) > THREADS_CHAR_LIMIT:
        compress = (
            f"That draft was {len(draft)} characters — over the "
            f"{THREADS_CHAR_LIMIT} limit. Rewrite shorter, same meaning. "
            f"Draft:\n{draft}"
        )
        draft = content_generation.generate_text(
            system_prompt=_response_system_prompt(persona_text),
            user_prompt=compress,
            max_output_tokens=400,
            providers=providers,
            client=client,
        )
    if len(draft) > THREADS_CHAR_LIMIT:
        raise ValueError(
            f"reply draft still over {THREADS_CHAR_LIMIT} chars after retry "
            f"({len(draft)} chars)"
        )
    return draft


def card_text(row: dict[str, Any]) -> str:
    """Telegram approval card body for one inbound reply."""
    parent = (row.get("parent_post_text") or "(our post)")[:140]
    theirs = (row.get("reply_text") or "(text unavailable)")[:280]
    ours = row.get("proposed_text") or ""
    return (
        "💬 NEW THREADS REPLY\n\n"
        f"Our post: {parent}\n\n"
        f"From: @{row.get('from_username') or 'unknown'}\n"
        f"Their reply:\n{theirs}\n\n"
        f"Suggested response ({len(ours)} chars):\n{ours}\n\n"
        f"Row #{row.get('id')}"
    )


def discover_new_replies(
    *,
    api: Any,
    store: Any,
    recent_posts: list[dict[str, Any]],
    own_username: str,
) -> list[dict[str, Any]]:
    """Fetch direct replies for recent own posts and upsert unseen ones.

    ``recent_posts`` items need {id, text, permalink}. Replies authored by
    the account itself are skipped. Returns only rows that are still in a
    state that needs work (discovered) — handled/rejected/ignored rows stay
    buried via the (account_key, reply_id) dedupe in upsert_own_reply.
    """
    new_rows: list[dict[str, Any]] = []
    for post in recent_posts:
        post_id = str(post.get("id") or "").strip()
        if not post_id:
            continue
        try:
            children = api.list_direct_replies(post_id)
        except Exception as exc:  # noqa: BLE001 - one bad post must not kill the sweep
            logger.warning("replies fetch failed for post %s: %s", post_id, exc)
            continue
        for child in children:
            child_id = str(child.get("id") or "").strip()
            if not child_id:
                continue
            child_username = (child.get("username") or "").strip() or None
            if child_username and own_username and child_username.lower() == own_username.lower():
                continue  # our own nested replies are not engagement targets
            row = store.upsert_own_reply(
                reply={
                    "reply_id": child_id,
                    "parent_post_id": post_id,
                    "parent_post_permalink": post.get("permalink"),
                    "parent_post_text": post.get("text"),
                    "from_username": child_username,
                    "reply_text": child.get("text"),
                    "replied_at": child.get("timestamp"),
                }
            )
            if row.get("status") == "discovered":
                new_rows.append(row)
    return new_rows
