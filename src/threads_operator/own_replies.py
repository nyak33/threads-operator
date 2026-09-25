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


# ---------------------------------------------------------------------------
# Task 2C — DM draft generation
# ---------------------------------------------------------------------------

# A DM is not a public reply: 1:1, must not look like spam or a cold pitch.
DM_CHAR_LIMIT = 1000


def _dm_system_prompt(persona_text: str) -> str:
    return (
        "You draft ONE private direct message (DM) on Threads to someone who "
        "replied to the account's own post and showed real interest.\n\n"
        f"ACCOUNT PERSONA:\n{persona_text}\n\n"
        "RULES:\n"
        "- Write in the persona's voice and language (Malay-first for syaqir).\n"
        "- 1-4 short sentences. Warm, personal, no corporate tone, no hype.\n"
        "- Acknowledge THEIR actual reply — reference what they said.\n"
        "- If a CTA / offer / business objective is given, carry it forward "
        "naturally (e.g. share the link, explain the next step). If none is "
        "given, do NOT invent one.\n"
        "- No aggressive sales language, no pressure, no fake urgency.\n"
        "- Never fabricate facts, prices, or promises the context does not state.\n"
        "- This is a private 1:1 message, not a public post.\n"
        f"- HARD LIMIT: {DM_CHAR_LIMIT} characters.\n"
        "- Output ONLY the DM text. No preamble, no quotes."
    )


def generate_dm_draft(
    *,
    store: Any,
    opportunity: dict[str, Any],
    persona_text: str,
    providers: list[content_generation.LLMProviderSpec] | None = None,
    client: Any = None,
) -> str:
    """Generate the DM draft for a DM opportunity (Hermes LLM layer).

    Threads Operator supplies the structured context (persona, root post,
    CTA, the incoming reply, intent, lead score); Hermes owns the wording.
    No provider credentials live here — ``content_generation`` resolves them.
    The returned text is persisted verbatim by the caller.
    """
    root_post = opportunity.get("root_post_text") or "(original post text unavailable)"
    reply = opportunity.get("reply_text") or "(their reply text unavailable)"
    username = opportunity.get("from_username") or "someone"
    intent = opportunity.get("intent") or "(intent unknown)"
    lead = opportunity.get("lead_score")
    lead_str = f"{float(lead):.2f}" if isinstance(lead, (int, float)) else "(n/a)"
    cta = opportunity.get("cta_matched")

    cta_block = (
        f"CTA / offer they responded to:\n{cta}\n\n"
        if cta
        else "(no specific CTA was matched — keep the message conversational, "
        "do not push an offer)\n\n"
    )

    user_prompt = (
        f"Their Threads username: @{username}\n\n"
        f"Our original post:\n{str(root_post)[:600]}\n\n"
        f"Their reply to it:\n{str(reply)[:600]}\n\n"
        f"Detected intent: {intent}\n"
        f"Lead score: {lead_str}\n\n"
        f"{cta_block}"
        "Draft the DM."
    )
    draft = content_generation.generate_text(
        system_prompt=_dm_system_prompt(persona_text),
        user_prompt=user_prompt,
        max_output_tokens=500,
        providers=providers,
        client=client,
    ).strip()
    if len(draft) > DM_CHAR_LIMIT:
        compress = (
            f"That DM was {len(draft)} characters — over the {DM_CHAR_LIMIT} "
            f"limit. Rewrite shorter, same meaning, same voice.\nDM:\n{draft}"
        )
        draft = content_generation.generate_text(
            system_prompt=_dm_system_prompt(persona_text),
            user_prompt=compress,
            max_output_tokens=500,
            providers=providers,
            client=client,
        ).strip()
    if len(draft) > DM_CHAR_LIMIT:
        raise ValueError(
            f"DM draft still over {DM_CHAR_LIMIT} chars after retry "
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


def classify_reply(
    *,
    store: Any,
    account_key: str,
    reply_row: dict[str, Any],
    persona_text: str | None = None,
    cta_patterns: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble context + classify intent for one reply row.

    This is the deterministic intelligence layer: it assembles structured
    context, classifies intent, and optionally creates a DM opportunity.
    It does NOT send any DM — it only prepares the data for the
    Telegram/browser layers.

    Returns a dict with:
    - context: the assembled ReplyContext
    - classification: the IntentClassification
    - dm_opportunity: the DMOppotunityResult (if created)
    - updated_row: the updated reply row with context/intent fields
    """
    from . import dm_opportunity, reply_context, reply_intent

    # 1. Assemble deterministic context
    context = reply_context.assemble_reply_context(
        store=store,
        account_key=account_key,
        reply_row=reply_row,
        persona_text=persona_text,
        cta_patterns=cta_patterns,
    )

    # 2. Classify intent
    classification = reply_intent.classify_reply_intent(
        context=context,
        reply_text=reply_row.get("reply_text"),
        post_cta=context.post_cta,
        cta_patterns=cta_patterns,
    )

    # 3. Persist context + intent on the reply row
    updated_row = store.update_own_reply(
        row_id=int(reply_row["id"]),
        fields={
            "context_json": context.to_dict(),
            "intent": classification.intent,
            "intent_evidence": classification.evidence.to_dict(),
            "lead_score": classification.lead_score,
            "classified_at": store._utc_now(),
        },
    )

    # 4. Optionally create DM opportunity
    dm_result = None
    if reply_intent.should_create_dm_opportunity(classification):
        dm_result = dm_opportunity.create_dm_opportunity(
            store=store,
            account_key=account_key,
            context=context,
            classification=classification,
        )
        if dm_result.success and dm_result.opportunity_id:
            dm_opportunity.link_reply_to_dm_opportunity(
                store=store,
                reply_row_id=int(reply_row["id"]),
                dm_opportunity_id=dm_result.opportunity_id,
            )

    return {
        "context": context,
        "classification": classification,
        "dm_opportunity": dm_result,
        "updated_row": updated_row,
    }
