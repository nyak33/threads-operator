"""Deterministic intent classification for Workflow B (own-post replies).

Classifies incoming replies into a structured intent taxonomy and decides
whether a DM opportunity should be created. Uses contextual evidence
(keyword/rule matching, CTA patterns, prior interactions) as deterministic
evidence — ambiguous cases fail conservatively to `needs_human_review`.

Intent taxonomy:
- casual: general conversation, no specific ask
- question: direct question about the post topic
- positive_engagement: praise, agreement, thanks
- objection: disagreement, criticism, concern
- buying_intent: explicit purchase interest, price inquiry
- cta_match: matched a CTA pattern in the post
- potential_lead: strong lead signal (CTA match + buying intent)
- needs_human_review: ambiguous, unclear, or potentially sensitive

The classifier never auto-approves a DM. It produces a classification
result that the operator stores and the Telegram layer consumes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .reply_context import ReplyContext

logger = logging.getLogger(__name__)

# --- Intent taxonomy ---
INTENT_CASUAL = "casual"
INTENT_QUESTION = "question"
INTENT_POSITIVE = "positive_engagement"
INTENT_OBJECTION = "objection"
INTENT_BUYING = "buying_intent"
INTENT_CTA_MATCH = "cta_match"
INTENT_POTENTIAL_LEAD = "potential_lead"
INTENT_NEEDS_REVIEW = "needs_human_review"

ALL_INTENTS = frozenset({
    INTENT_CASUAL,
    INTENT_QUESTION,
    INTENT_POSITIVE,
    INTENT_OBJECTION,
    INTENT_BUYING,
    INTENT_CTA_MATCH,
    INTENT_POTENTIAL_LEAD,
    INTENT_NEEDS_REVIEW,
})

# --- Deterministic evidence patterns (Malay + English) ---
# These are evidence signals, not auto-classifiers. Multiple signals combine
# to produce a classification; ambiguous combinations fall to needs_human_review.

_QUESTION_PATTERNS = frozenset({
    "berapa", "macam mana", "bagaimana", "boleh ke", "boleh tak",
    "kenapa", "mengapa", "apakah", "siapa", "bila", "di mana",
    "what", "how much", "how to", "why", "when", "where", "who",
    "can i", "could i", "is it", "are there", "do you", "does it",
})

_POSITIVE_PATTERNS = frozenset({
    "terbaik", "bagus", "hebat", "power", "nice", "good", "great",
    "awesome", "amazing", "love it", "suka", "thumbs up", "respect",
    "setuju", "agree", "on point", "well said", "inspiring",
    "motivasi", "inspirasi", "keep it up", "teruskan", "proud",
})

_OBJECTION_PATTERNS = frozenset({
    "tak betul", "salah", "bohong", "tipu", "scam", "penipu",
    "overpriced", "mahal sangat", "tak berbaloi", "not worth",
    "disagree", "tak setuju", "wrong", "false", "fake",
    "tak boleh pakai", "useless", "waste of money", "menipu",
})

_BUYING_PATTERNS = frozenset({
    "berapa harga", "harga", "price", "cost", "bayar", "payment",
    "beli", "buy", "order", "tempah", "booking", "subscribe",
    "daftar", "register", "sign up", "join", "nak try", "want to try",
    "interested", "serius", "serious", "confirm",
})

# CTA match patterns — these are matched against the reply text AND
# checked against the post's CTA. A reply that matches a CTA pattern
# from the post is classified as cta_match.
_CTA_REPLY_PATTERNS = frozenset({
    "berminat", "minat", "nak", "mahu", "want", "interested", "yes please",
    "boleh", "can", "ok", "okay", "yes", "ya", "setuju",
    "dm saya", "pm", "message me", "link please", "send link",
    "share link", "beri link", "hantar link",
})

# Ambiguous patterns that could be casual OR buying intent
_AMBIGUOUS_PATTERNS = frozenset({
    "ok", "okay", "nice", "bagus", "terbaik", "good",
    "boleh", "can", "ya", "yes",
})


@dataclass(frozen=True)
class IntentEvidence:
    """Deterministic evidence collected during classification."""
    matched_patterns: tuple[str, ...]
    evidence_type: str  # "keyword", "cta_match", "contextual", "composite"
    confidence: float  # 0.0-1.0
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_patterns": list(self.matched_patterns),
            "evidence_type": self.evidence_type,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class IntentClassification:
    """Result of intent classification."""
    intent: str
    evidence: IntentEvidence
    lead_score: float  # 0.0-1.0
    dm_opportunity: bool  # Whether this should create a DM opportunity
    requires_human: bool  # Whether Hermes/human review is needed
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "evidence": self.evidence.to_dict(),
            "lead_score": self.lead_score,
            "dm_opportunity": self.dm_opportunity,
            "requires_human": self.requires_human,
            "reason": self.reason,
        }


def _match_patterns(text: str, patterns: frozenset[str]) -> list[str]:
    """Find which patterns match the given text (case-insensitive)."""
    if not text:
        return []
    text_lower = text.lower().strip()
    matched = []
    for pattern in patterns:
        if pattern in text_lower:
            matched.append(pattern)
    return matched


def _is_cta_match(reply_text: str, post_cta: str | None, cta_patterns: tuple[str, ...]) -> bool:
    """Determine if the reply matches a CTA from the original post.

    A CTA match requires:
    1. The reply text contains a CTA reply pattern (e.g. "berminat", "nak")
    2. AND either:
       a. The post has an explicit CTA that the reply aligns with, OR
       b. The reply matches one of the configured CTA patterns
    """
    if not reply_text:
        return False

    reply_lower = reply_text.lower().strip()
    reply_matches = _match_patterns(reply_lower, _CTA_REPLY_PATTERNS)
    if not reply_matches:
        return False

    # If the post has an explicit CTA, check alignment
    if post_cta:
        post_cta_lower = post_cta.lower()
        # The reply should be semantically aligned with the CTA
        for pattern in reply_matches:
            if pattern in post_cta_lower or pattern in reply_lower:
                return True

    # If no explicit post CTA, check configured CTA patterns
    for pattern in cta_patterns:
        if pattern.lower() in reply_lower:
            return True

    # Fallback: reply matches a CTA reply pattern but no explicit alignment
    # This is a weak signal — still counts as CTA match but with lower confidence
    return True


def classify_reply_intent(
    *,
    context: ReplyContext,
    reply_text: str | None,
    post_cta: str | None = None,
    cta_patterns: tuple[str, ...] = (),
) -> IntentClassification:
    """Classify the intent of an incoming reply.

    Args:
        context: The assembled ReplyContext for this reply.
        reply_text: The text of the incoming reply.
        post_cta: The CTA extracted from the original post.
        cta_patterns: Configured CTA patterns from engagement config.

    Returns:
        IntentClassification with intent, evidence, lead_score, and
        dm_opportunity flag.
    """
    if not reply_text or not reply_text.strip():
        return IntentClassification(
            intent=INTENT_NEEDS_REVIEW,
            evidence=IntentEvidence(
                matched_patterns=(),
                evidence_type="contextual",
                confidence=0.0,
                notes=("empty_reply_text",),
            ),
            lead_score=0.0,
            dm_opportunity=False,
            requires_human=True,
            reason="Reply text is empty or blank — cannot classify",
        )

    text = reply_text.strip()
    text_lower = text.lower()

    # --- Collect deterministic evidence ---
    question_matches = _match_patterns(text_lower, _QUESTION_PATTERNS)
    positive_matches = _match_patterns(text_lower, _POSITIVE_PATTERNS)
    objection_matches = _match_patterns(text_lower, _OBJECTION_PATTERNS)
    buying_matches = _match_patterns(text_lower, _BUYING_PATTERNS)
    cta_reply_matches = _match_patterns(text_lower, _CTA_REPLY_PATTERNS)
    cta_match = _is_cta_match(text, post_cta, cta_patterns)

    # Check for prior interactions (contextual evidence)
    has_prior_interactions = context.prior_interaction_count > 0

    # --- Composite classification ---
    # CTA match + buying = potential lead (highest priority for lead gen)
    if buying_matches and cta_match:
        return IntentClassification(
            intent=INTENT_POTENTIAL_LEAD,
            evidence=IntentEvidence(
                matched_patterns=tuple(buying_matches + ["cta_match"]),
                evidence_type="composite",
                confidence=0.85,
                notes=(
                    "buying_intent_patterns_matched",
                    "cta_aligned",
                    f"prior_interactions={context.prior_interaction_count}",
                ),
            ),
            lead_score=0.85,
            dm_opportunity=True,
            requires_human=False,
            reason="Strong buying intent with CTA alignment — potential lead",
        )

    # CTA match alone (without buying patterns)
    if cta_match:
        # Check if the reply is just an ambiguous acknowledgment
        ambiguous_matches = _match_patterns(text_lower, _AMBIGUOUS_PATTERNS)
        if ambiguous_matches and len(text_lower.split()) <= 3:
            # Short ambiguous reply matching CTA — conservative: needs review
            return IntentClassification(
                intent=INTENT_CTA_MATCH,
                evidence=IntentEvidence(
                    matched_patterns=tuple(dict.fromkeys(ambiguous_matches + cta_reply_matches + ["cta_match"])),
                    evidence_type="composite",
                    confidence=0.6,
                    notes=(
                        "ambiguous_short_reply",
                        "cta_pattern_matched",
                        "short_acknowledgment",
                    ),
                ),
                lead_score=0.6,
                dm_opportunity=True,
                requires_human=False,
                reason="CTA pattern matched in short reply — DM opportunity created",
            )
        else:
            return IntentClassification(
                intent=INTENT_CTA_MATCH,
                evidence=IntentEvidence(
                    matched_patterns=tuple(dict.fromkeys(cta_reply_matches + ["cta_match"])),
                    evidence_type="keyword",
                    confidence=0.7,
                    notes=("cta_pattern_matched",),
                ),
                lead_score=0.7,
                dm_opportunity=True,
                requires_human=False,
                reason="CTA pattern matched — DM opportunity created",
            )

    # Question with buying words but no CTA = still a question
    # e.g. "Berapa harga untuk setup ads?" — asking for info, not stating intent
    if question_matches and buying_matches and not cta_match:
        return IntentClassification(
            intent=INTENT_QUESTION,
            evidence=IntentEvidence(
                matched_patterns=tuple(question_matches + buying_matches),
                evidence_type="composite",
                confidence=0.6,
                notes=("question_with_buying_words", "information_seeking"),
            ),
            lead_score=0.4,
            dm_opportunity=False,
            requires_human=False,
            reason="Price/information question — no DM opportunity",
        )

    # Question (only when no CTA/buying signals — pure question)
    if question_matches and not buying_matches and not cta_match:
        return IntentClassification(
            intent=INTENT_QUESTION,
            evidence=IntentEvidence(
                matched_patterns=tuple(question_matches),
                evidence_type="keyword",
                confidence=0.8,
                notes=("question_patterns_matched",),
            ),
            lead_score=0.3,
            dm_opportunity=False,
            requires_human=False,
            reason="Direct question — no DM opportunity",
        )

    # Strong buying intent without CTA alignment
    if buying_matches:
        return IntentClassification(
            intent=INTENT_BUYING,
            evidence=IntentEvidence(
                matched_patterns=tuple(buying_matches),
                evidence_type="keyword",
                confidence=0.7,
                notes=("buying_intent_patterns_matched",),
            ),
            lead_score=0.7,
            dm_opportunity=True,
            requires_human=False,
            reason="Buying intent detected without explicit CTA match",
        )

    # Objection
    if objection_matches:
        return IntentClassification(
            intent=INTENT_OBJECTION,
            evidence=IntentEvidence(
                matched_patterns=tuple(objection_matches),
                evidence_type="keyword",
                confidence=0.8,
                notes=("objection_patterns_matched",),
            ),
            lead_score=0.0,
            dm_opportunity=False,
            requires_human=True,
            reason="Objection detected — may need human attention",
        )

    # Positive engagement
    if positive_matches:
        return IntentClassification(
            intent=INTENT_POSITIVE,
            evidence=IntentEvidence(
                matched_patterns=tuple(positive_matches),
                evidence_type="keyword",
                confidence=0.7,
                notes=("positive_patterns_matched",),
            ),
            lead_score=0.1,
            dm_opportunity=False,
            requires_human=False,
            reason="Positive engagement — no DM opportunity",
        )

    # Ambiguous short reply that matches an acknowledgment pattern
    ambiguous_matches = _match_patterns(text_lower, _AMBIGUOUS_PATTERNS)
    if ambiguous_matches and len(text_lower.split()) <= 5:
        return IntentClassification(
            intent=INTENT_CASUAL,
            evidence=IntentEvidence(
                matched_patterns=tuple(ambiguous_matches),
                evidence_type="keyword",
                confidence=0.5,
                notes=("short_acknowledgment", "ambiguous"),
            ),
            lead_score=0.2,
            dm_opportunity=False,
            requires_human=False,
            reason="Short acknowledgment — casual, no DM opportunity",
        )

    # Default: casual with low confidence
    return IntentClassification(
        intent=INTENT_CASUAL,
        evidence=IntentEvidence(
            matched_patterns=(),
            evidence_type="contextual",
            confidence=0.3,
            notes=("no_strong_patterns_matched",),
        ),
        lead_score=0.1,
        dm_opportunity=False,
        requires_human=False,
        reason="No strong intent patterns — treated as casual",
    )


def should_create_dm_opportunity(classification: IntentClassification) -> bool:
    """Deterministic gate: should this classification create a DM opportunity?

    Only classifications with dm_opportunity=True AND sufficient confidence
    create a DM opportunity. This is the fail-closed gate.
    """
    if not classification.dm_opportunity:
        return False
    if classification.evidence.confidence < 0.5:
        return False
    if classification.intent not in (INTENT_CTA_MATCH, INTENT_BUYING, INTENT_POTENTIAL_LEAD):
        return False
    return True
