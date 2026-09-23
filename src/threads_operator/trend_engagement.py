"""Workflow A: trend candidate -> ORIGINAL own post (approval-gated).

State machine on ``threads_trend_candidates`` (extended by migration 010):

    discovered -> drafted -> pending_approval -> approved -> queued -> posted
                                                 -> rejected (terminal)
                                                 -> skipped   (terminal)
                                                 -> failed    (retryable via redraft)

Everything is derived from existing operator infrastructure:
- candidates come from the existing trend-discovery collectors;
- drafts are generated with the account persona (personas/<account>.md) via the
  provider-agnostic LLM chain in content_generation.py;
- approval rides the existing Telebot inline-button architecture
  (callback prefix ``trendeng:``);
- approval enqueues through the EXISTING publish-queue ingress
  (SupabaseStore.enqueue_draft) so the normal scheduler/publisher owns actual
  publication. This module never publishes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import content_generation

logger = logging.getLogger(__name__)

THREADS_CHAR_LIMIT = 500

DEFAULT_THEMES = (
    "digital marketing",
    "upskilling / career",
    "sales",
    "entrepreneurship / business",
    "AI / automation",
    "customer data",
    "packaging / SmartKood / business systems",
    "practical working-life observations",
)

DEFAULT_REJECT_THEMES = (
    "celebrity gossip",
    "political content",
    "irrelevant viral drama",
)

_WORKFLOW_A_STATUSES = {"discovered", "drafted", "pending_approval"}
_PROPOSABLE_STATUSES = ("discovered", "drafted")


@dataclass(frozen=True)
class TrendProposal:
    candidate_id: int
    draft_text: str
    topic: str
    relevance_score: float
    reason: str
    angle: str


class PersonaMissingError(RuntimeError):
    """Raised when personas/<account>.md is absent — generation must stop."""


def load_persona(personas_root: Path, account_key: str) -> str:
    """Load the exact persona for an account. No cross-account fallback."""
    path = personas_root / f"{account_key}.md"
    if not path.exists():
        raise PersonaMissingError(
            f"persona file missing for account '{account_key}': {path}"
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise PersonaMissingError(f"persona file is empty: {path}")
    return text


def _filter_system_prompt(themes: tuple[str, ...], reject_themes: tuple[str, ...]) -> str:
    theme_lines = "\n".join(f"- {t}" for t in themes)
    reject_lines = "\n".join(f"- {t}" for t in reject_themes)
    return (
        "You score Threads trend candidates for one specific account.\n"
        "Relevant themes for this account:\n"
        f"{theme_lines}\n\n"
        "Reject themes (always score these 0.0-0.2):\n"
        f"{reject_lines}\n"
        "- anything that does not fit the account persona\n\n"
        "Respond with ONLY a JSON object, no prose, no code fences:\n"
        '{"score": <float 0.0-1.0>, "topic": "<2-4 word topic tag in English", '
        '"reason": "<one sentence in Malay explaining why it fits the account>", '
        '"angle": "<one sentence in Malay: the original angle the account could take, '
        'inspired by but not copying the source>", '
        '"relevant": <true if score >= threshold else false>}'
    )


def score_candidate(
    candidate: dict[str, Any],
    *,
    persona_text: str,
    themes: tuple[str, ...] = DEFAULT_THEMES,
    reject_themes: tuple[str, ...] = DEFAULT_REJECT_THEMES,
    threshold: float = 0.6,
    providers: list[content_generation.LLMProviderSpec] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Score one candidate against the account themes. Returns the parsed verdict."""
    source_text = (candidate.get("source_text") or "")[:1200]
    username = candidate.get("source_username") or "unknown"
    user_prompt = (
        f"Account persona (context for fit):\n{persona_text[:1500]}\n\n"
        f"Trend candidate by @{username}:\n{source_text}\n\n"
        f"Relevance threshold: {threshold}\n"
        "Score this candidate."
    )
    verdict = content_generation.generate_json(
        system_prompt=_filter_system_prompt(themes, reject_themes),
        user_prompt=user_prompt,
        providers=providers,
        client=client,
    )
    try:
        score = float(verdict.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    verdict["score"] = max(0.0, min(1.0, score))
    verdict["relevant"] = bool(verdict.get("relevant")) and verdict["score"] >= threshold
    for field in ("topic", "reason", "angle"):
        verdict[field] = str(verdict.get(field) or "").strip()
    return verdict


def _draft_system_prompt(persona_text: str) -> str:
    return (
        "You write ONE original Threads post for the account described below.\n\n"
        f"ACCOUNT PERSONA:\n{persona_text}\n\n"
        "RULES:\n"
        "- The post is INSPIRED BY a trend's angle, never a copy or close paraphrase.\n"
        "- It must stand alone: a reader who never saw the trend must get full value.\n"
        "- Follow the persona's voice, language, and length discipline exactly.\n"
        f"- HARD LIMIT: {THREADS_CHAR_LIMIT} characters maximum.\n"
        "- Never fabricate first-person experience the persona does not claim.\n"
        "- Output ONLY the post text. No preamble, no explanation, no quotes."
    )


def generate_original_draft(
    candidate: dict[str, Any],
    verdict: dict[str, Any],
    *,
    persona_text: str,
    providers: list[content_generation.LLMProviderSpec] | None = None,
    client: Any = None,
) -> str:
    """Generate one original standalone post. Fails loudly over the char limit."""
    user_prompt = (
        f"Trend angle to recreate in your own voice: {verdict.get('angle') or ''}\n"
        f"Why it fits this account: {verdict.get('reason') or ''}\n"
        f"Source post (REFERENCE ONLY — do not copy wording):\n"
        f"{(candidate.get('source_text') or '')[:800]}\n\n"
        "Write the post now."
    )
    draft = content_generation.generate_text(
        system_prompt=_draft_system_prompt(persona_text),
        user_prompt=user_prompt,
        providers=providers,
        client=client,
    )
    if len(draft) > THREADS_CHAR_LIMIT:
        # One compression retry with the overage stated plainly.
        compress_prompt = (
            f"Your previous draft was {len(draft)} characters — over the "
            f"{THREADS_CHAR_LIMIT} limit. Rewrite it under the limit, same angle, "
            f"same voice. Previous draft:\n{draft}"
        )
        draft = content_generation.generate_text(
            system_prompt=_draft_system_prompt(persona_text),
            user_prompt=compress_prompt,
            providers=providers,
            client=client,
        )
    if len(draft) > THREADS_CHAR_LIMIT:
        raise ValueError(
            f"draft still over {THREADS_CHAR_LIMIT} chars after compression retry "
            f"({len(draft)} chars)"
        )
    return draft


def trend_candidate_payload(
    candidate: dict[str, Any],
    verdict: dict[str, Any],
    draft_text: str,
) -> dict[str, Any]:
    """Fields persisted back onto threads_trend_candidates for a drafted row."""
    raw = dict(candidate.get("raw_metadata") or {})
    raw["workflow_a"] = {
        "draft_text": draft_text,
        "relevance_score": verdict.get("score"),
        "reason": verdict.get("reason"),
        "angle": verdict.get("angle"),
        "topic": verdict.get("topic"),
    }
    return {
        "status": "pending_approval",
        "topic": verdict.get("topic") or candidate.get("topic"),
        "adaptation_angle": verdict.get("angle") or candidate.get("adaptation_angle"),
        "why_it_works": verdict.get("reason") or candidate.get("why_it_works"),
        "trend_score": verdict.get("score"),
        "raw_metadata": raw,
    }


def card_text(candidate: dict[str, Any], proposal: dict[str, Any]) -> str:
    """Telegram approval card body (no credentials, no callback data)."""
    draft = proposal.get("draft_text") or ""
    return (
        "🆕 TREND CANDIDATE\n\n"
        f"Source: @{candidate.get('source_username') or 'unknown'}\n"
        f"URL: {candidate.get('source_permalink') or 'n/a'}\n"
        f"Reason: {proposal.get('reason') or 'n/a'}\n"
        f"Suggested angle: {proposal.get('angle') or 'n/a'}\n\n"
        f"Draft ({len(draft)} chars):\n{draft}\n\n"
        f"Topic: {proposal.get('topic') or 'n/a'}\n"
        f"Score: {proposal.get('relevance_score', 0):.2f}\n"
        f"Candidate #{candidate.get('id')}"
    )


def refresh_original_draft(
    candidate: dict[str, Any],
    *,
    persona_text: str,
    providers: list[content_generation.LLMProviderSpec] | None = None,
    client: Any = None,
) -> str:
    """Rewrite the backlog draft using the original context/source.

    The provider chain is resolved dynamically from config — never hardcoded.
    The result is still bounded by the Threads character limit.
    """
    wa = (candidate.get("raw_metadata") or {}).get("workflow_a") or {}
    user_prompt = (
        f"Original trend angle: {wa.get('angle') or ''}\n"
        f"Why it fits this account: {wa.get('reason') or ''}\n"
        f"Previous draft (for tone reference only):\n{wa.get('draft_text') or ''}\n"
        f"Source post (REFERENCE ONLY — do not copy wording):\n"
        f"{(candidate.get('source_text') or '')[:800]}\n\n"
        "Write a fresh standalone post now."
    )
    draft = content_generation.generate_text(
        system_prompt=_draft_system_prompt(persona_text),
        user_prompt=user_prompt,
        providers=providers,
        client=client,
    )
    if len(draft) > THREADS_CHAR_LIMIT:
        compress_prompt = (
            f"Your previous draft was {len(draft)} characters — over the "
            f"{THREADS_CHAR_LIMIT} limit. Rewrite it under the limit, same angle, "
            f"same voice. Previous draft:\n{draft}"
        )
        draft = content_generation.generate_text(
            system_prompt=_draft_system_prompt(persona_text),
            user_prompt=compress_prompt,
            providers=providers,
            client=client,
        )
    if len(draft) > THREADS_CHAR_LIMIT:
        raise ValueError(
            f"draft still over {THREADS_CHAR_LIMIT} chars after compression retry "
            f"({len(draft)} chars)"
        )
    return draft
