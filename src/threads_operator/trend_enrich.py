"""Trend-candidate enrichment: permalink -> authenticated read -> evidence ->
account-scoped factual UPDATE of allowlisted evidence fields.

Design (approved, v2):
  candidate permalink -> browser read (trend_browser, read-only CDP reuse)
  -> Relay/__bbox structured facts (trend_enrich_extract, fail-closed,
     observed-fields-only)
  -> SupabaseStore.update_trend_candidate_evidence (allowlisted PATCH,
     account-scoped WHERE id AND target_account_id, raw_metadata merge)

Hard rules:
- only factual, observed evidence is patched: source_post_id,
  source_username, source_text, published_at, likes, replies, reposts,
  quotes, last_checked_at; enrichment facts (enrichment_source,
  media_type, self_thread_length) merge into raw_metadata;
- views, status, topic, and every analysis field are structurally
  unreachable from this module — and rejected again by the store;
- absent facts are never written as NULL; a later payload omission cannot
  erase previously known evidence;
- last_checked_at advances only after a successful validated extraction;
  any extraction failure raises and NOTHING is written;
- semantic idempotency: when every observed evidence field already equals
  the row's value, the run is a no-op (writes 0).
"""
from __future__ import annotations

import datetime as _dt
import pathlib
from typing import Any

from .trend_browser import read_post_document_sync
from .trend_enrich_extract import (
    TrendChallengeError,
    TrendEnrichError,
    expected_shortcode,
    extract_post_facts,
)

__all__ = [
    "TrendChallengeError",
    "TrendEnrichError",
    "enrich_candidate",
    "enrich_candidate_dry_run",
    "enrich_permalink_dry_run",
    "build_evidence_patch",
]

_EVIDENCE_KEYS = (
    "source_post_id",
    "source_username",
    "source_text",
    "published_at",
    "likes",
    "replies",
    "reposts",
    "quotes",
)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def build_evidence_patch(facts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split extracted facts into (evidence PATCH, raw_metadata enrichment).

    Only observed keys go in; nothing is defaulted or nulled.
    """
    evidence = {k: facts[k] for k in _EVIDENCE_KEYS if k in facts}
    enrichment: dict[str, Any] = {"enrichment_source": "permalink_preloader"}
    for extra in ("media_type", "self_thread_length"):
        if extra in facts:
            enrichment[extra] = facts[extra]
    return evidence, enrichment


def _require_permalink(candidate: dict[str, Any]) -> str:
    permalink = candidate.get("source_permalink") or ""
    if not permalink:
        raise TrendEnrichError("candidate has no source_permalink to enrich from")
    return str(permalink)


def _read_facts(candidate: dict[str, Any], profile_dir: pathlib.Path,
                settle_seconds: float) -> tuple[str, str, dict[str, Any]]:
    platform = candidate.get("source_platform")
    if platform not in (None, "threads"):
        raise TrendEnrichError(
            f"candidate source_platform {platform!r} is not 'threads' — refuse")
    permalink = _require_permalink(candidate)
    shortcode = expected_shortcode(permalink)
    document = read_post_document_sync(profile_dir, permalink,
                                       settle_seconds=settle_seconds)
    facts = extract_post_facts(document, shortcode)
    return permalink, shortcode, facts


def enrich_candidate(store,
                     *,
                     candidate_id: int,
                     profile_dir: pathlib.Path,
                     settle_seconds: float = 3.0) -> dict[str, Any]:
    """Read candidate -> browser facts -> allowlisted factual UPDATE.

    Returns {"updated", "candidate_id", "source_post_id", "code",
    "evidence"}. Raises TrendChallengeError/TrendEnrichError without
    writing on ANY ambiguous or failed extraction.
    """
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        raise TrendEnrichError(
            f"trend candidate {candidate_id} not found for this account")

    _permalink, shortcode, facts = _read_facts(candidate, profile_dir,
                                               settle_seconds)
    evidence, enrichment = build_evidence_patch(facts)
    evidence["last_checked_at"] = _utc_now_iso()

    # Semantic idempotency: if every observed fact already equals the row,
    # skip the PATCH entirely (repeatable refresh without churn).
    if all(candidate.get(k) == v for k, v in evidence.items()
           if k != "last_checked_at"):
        return {
            "updated": False,
            "candidate_id": candidate_id,
            "source_post_id": evidence["source_post_id"],
            "code": shortcode,
            "evidence": {k: v for k, v in evidence.items()},
        }

    row = store.update_trend_candidate_evidence(
        candidate_id=candidate_id, evidence=evidence, enrichment=enrichment)
    if row is None:
        raise TrendEnrichError(
            "account-scoped update matched no row — concurrent change, STOP")
    return {
        "updated": True,
        "candidate_id": candidate_id,
        "source_post_id": evidence["source_post_id"],
        "code": shortcode,
        "evidence": {k: v for k, v in evidence.items()},
    }


def enrich_candidate_dry_run(store,
                             *,
                             candidate_id: int,
                             profile_dir: pathlib.Path,
                             settle_seconds: float = 3.0) -> dict[str, Any]:
    """Dry-run by candidate id: real account-scoped read + browser extract,
    ZERO writes. store is used ONLY for get_trend_candidate."""
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        raise TrendEnrichError(
            f"trend candidate {candidate_id} not found for this account")
    _permalink, shortcode, facts = _read_facts(candidate, profile_dir,
                                               settle_seconds)
    evidence, enrichment = build_evidence_patch(facts)
    return {
        "dry_run": True,
        "writes": 0,
        "candidate_id": candidate_id,
        "expected_shortcode": shortcode,
        "extraction_source": "permalink_preloader",
        "evidence": {**evidence, "last_checked_at": "<now-utc-on-write>"},
        "raw_metadata_merge_keys": sorted(enrichment),
    }


def enrich_permalink_dry_run(*,
                             permalink: str,
                             profile_dir: pathlib.Path,
                             settle_seconds: float = 3.0) -> dict[str, Any]:
    """Dry-run by URL: browser read + extraction only. Never touches Supabase."""
    shortcode = expected_shortcode(permalink)
    document = read_post_document_sync(profile_dir, permalink,
                                       settle_seconds=settle_seconds)
    facts = extract_post_facts(document, shortcode)
    evidence, enrichment = build_evidence_patch(facts)
    return {
        "dry_run": True,
        "writes": 0,
        "permalink": permalink,
        "expected_shortcode": shortcode,
        "extraction_source": "permalink_preloader",
        "evidence": evidence,
        "raw_metadata_merge_keys": sorted(enrichment),
    }
