"""Trend-candidate enrichment: permalink -> authenticated read -> evidence ->
account-scoped factual UPDATE of source_post_id ONLY.

Design (approved, v1):
  candidate permalink -> browser read (trend_browser, read-only CDP reuse)
  -> Relay/__bbox structured evidence (trend_enrich_extract, fail-closed)
  -> SupabaseStore.update_trend_candidate_source_post_id (allowlisted PATCH)

Hard rules:
- no status change, no analysis fields, no view updates, no OG fallback,
  no LLM — anything short of an exact structured match raises and NOTHING
  is written;
- the write is account-scoped twice: the row is read via get_trend_candidate
  (id + target_account_id), and the PATCH filters on both again.
"""
from __future__ import annotations

import pathlib
from typing import Any

from .trend_browser import read_post_document_sync
from .trend_enrich_extract import (
    TrendChallengeError,
    TrendEnrichError,
    expected_shortcode,
    extract_post_evidence,
)

__all__ = [
    "TrendChallengeError",
    "TrendEnrichError",
    "enrich_candidate",
    "enrich_permalink_dry_run",
]


def _require_permalink(candidate: dict[str, Any]) -> str:
    permalink = candidate.get("source_permalink") or ""
    if not permalink:
        raise TrendEnrichError("candidate has no source_permalink to enrich from")
    return str(permalink)


def enrich_candidate(store,
                     *,
                     candidate_id: int,
                     profile_dir: pathlib.Path,
                     settle_seconds: float = 3.0) -> dict[str, Any]:
    """Read candidate -> browser evidence -> factual UPDATE (source_post_id).

    Returns {"updated", "candidate_id", "source_post_id", "code"}.
    Raises TrendChallengeError/TrendEnrichError without writing on ANY
    ambiguous evidence.
    """
    candidate = store.get_trend_candidate(candidate_id)
    if candidate is None:
        raise TrendEnrichError(
            f"trend candidate {candidate_id} not found for this account")

    permalink = _require_permalink(candidate)
    shortcode = expected_shortcode(permalink)
    document = read_post_document_sync(profile_dir, permalink,
                                       settle_seconds=settle_seconds)
    evidence = extract_post_evidence(document, shortcode)

    if candidate.get("source_post_id") == evidence["pk"]:
        return {
            "updated": False,
            "candidate_id": candidate_id,
            "source_post_id": evidence["pk"],
            "code": evidence["code"],
        }

    row = store.update_trend_candidate_source_post_id(
        candidate_id=candidate_id, source_post_id=evidence["pk"])
    if row is None:
        raise TrendEnrichError(
            "account-scoped update matched no row — concurrent change, STOP")
    return {
        "updated": True,
        "candidate_id": candidate_id,
        "source_post_id": evidence["pk"],
        "code": evidence["code"],
    }


def enrich_permalink_dry_run(*,
                             permalink: str,
                             profile_dir: pathlib.Path,
                             settle_seconds: float = 3.0) -> dict[str, Any]:
    """Dry-run: browser read + extraction only. Never touches Supabase."""
    shortcode = expected_shortcode(permalink)
    document = read_post_document_sync(profile_dir, permalink,
                                       settle_seconds=settle_seconds)
    evidence = extract_post_evidence(document, shortcode)
    return {
        "dry_run": True,
        "writes": 0,
        "permalink": permalink,
        "evidence": {
            "code": evidence["code"],
            "source_post_id": evidence["pk"],
        },
    }
