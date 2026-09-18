"""Multi-source provenance for trend candidates — raw_metadata-only merge.

Representation inside ``raw_metadata`` (no schema change; legacy scalar
keys are preserved untouched and never used as the sole record once a row
is touched):

- ``candidate_roles``       list of analytical roles. Only roles PROVEN by
                            an event are added; Activity proves
                            ``own_performance``. ``external_trend`` is NEVER
                            added automatically (manual ingestion alone does
                            not imply it — that role belongs to a future
                            explicit external-discovery workflow).
- ``discovery_methods``     list, e.g. ["manual_url", "activity_attribution"]
- ``activity_attributions`` list of factual attribution dicts, deduped by
                            ``event_id`` (first write wins; facts about an
                            event row never mutate).

Legacy compatibility: a row carrying only the scalar ``candidate_role`` /
``discovery_method`` / ``activity_*`` fields from the pre-merge schema is
normalized on touch — the scalar is mirrored into the lists and scalar
activity fields are synthesized into ONE attribution entry (only from
fields actually present; nothing is fabricated). The historical scalars
themselves are kept verbatim for older readers, including a legacy
``external_trend`` label, which is treated as history, not proof.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "activity_attribution_from_event",
    "candidate_roles",
    "discovery_methods",
    "merge_activity_provenance",
    "provenance_for_insert",
]

OWN_PERFORMANCE = "own_performance"
EXTERNAL_TREND = "external_trend"
MANUAL_URL = "manual_url"
ACTIVITY_ATTRIBUTION = "activity_attribution"

# keys this module owns inside raw_metadata
PROVENANCE_KEYS = ("candidate_roles", "discovery_methods", "activity_attributions")

# canonical attribution entry key -> event row column
_EVENT_TO_ATTRIBUTION = (
    ("event_id", "id"),
    ("follower_username", "visible_username"),
    ("match_method", "match_method"),
    ("match_confidence", "match_confidence"),
    ("collected_at", "collected_at"),
)

# canonical attribution entry key -> legacy scalar mirror key
_SCALAR_MIRROR = {
    "event_id": "activity_event_id",
    "follower_username": "activity_follower_username",
    "match_method": "activity_match_method",
    "match_confidence": "activity_match_confidence",
    "collected_at": "activity_collected_at",
    "numeric_id": "activity_numeric_id",
}


def _clean_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def activity_attribution_from_event(
    event: dict[str, Any], numeric_id: str | None = None
) -> dict[str, Any]:
    """Build one factual attribution entry from a validated event row.

    Only non-sensitive observed facts; the follower username is attribution
    evidence, never a source author. Raises ValueError without a usable
    integer event id — an unattributable entry is never fabricated.
    """
    if not isinstance(event, dict):
        raise ValueError("activity event must be a dict")
    raw_id = event.get("id")
    if isinstance(raw_id, bool) or raw_id is None:
        raise ValueError("activity event has no id — refusing unattributable provenance")
    try:
        entry: dict[str, Any] = {"event_id": int(raw_id)}
    except (TypeError, ValueError):
        raise ValueError("activity event id is not an integer") from None
    for entry_key, column in _EVENT_TO_ATTRIBUTION:
        if entry_key == "event_id":
            continue
        value = _clean_str(event.get(column))
        if value is not None:
            entry[entry_key] = value
    numeric = _clean_str(numeric_id)
    if numeric is not None:
        entry["numeric_id"] = numeric
    return entry


def _as_role_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"raw_metadata.{field} must be a list of strings")
    return list(value)


def _dedup_extend(items: list[str], value: str) -> list[str]:
    if value not in items:
        items.append(value)
    return items


def _synthesize_legacy_attributions(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct ONE attribution entry from legacy scalar activity_* keys.

    Only fields that are actually present are included (no fabrication);
    without at least a usable event id there is nothing to synthesize.
    """
    raw_id = meta.get("activity_event_id")
    if raw_id is None or isinstance(raw_id, bool):
        return []
    try:
        entry: dict[str, Any] = {"event_id": int(raw_id)}
    except (TypeError, ValueError):
        return []
    for attr_key, scalar_key in _SCALAR_MIRROR.items():
        if attr_key == "event_id":
            continue
        value = _clean_str(meta.get(scalar_key))
        if value is not None:
            entry[attr_key] = value
    return [entry]


def merge_activity_provenance(
    raw_metadata: dict[str, Any] | None,
    attribution: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Return (merged_raw_metadata, changed) after one Activity attribution.

    Never mutates the input. Fails closed (ValueError) on malformed
    provenance lists or an entry without an integer ``event_id``. Idempotent:
    re-merging the same event_id changes nothing.
    """
    if raw_metadata is None:
        raw_metadata = {}
    if not isinstance(raw_metadata, dict):
        raise ValueError("raw_metadata must be a dict when present")
    if not isinstance(attribution, dict):
        raise ValueError("attribution must be a dict")
    event_id = attribution.get("event_id")
    if isinstance(event_id, bool) or not isinstance(event_id, int):
        raise ValueError("attribution requires an integer event_id")

    merged = dict(raw_metadata)

    roles = _as_role_list(merged.get("candidate_roles"), "candidate_roles")
    # Legacy scalar own_performance normalizes into the list; a legacy
    # external_trend scalar is history, NOT proof, and is never promoted.
    if merged.get("candidate_role") == OWN_PERFORMANCE:
        _dedup_extend(roles, OWN_PERFORMANCE)
    _dedup_extend(roles, OWN_PERFORMANCE)

    methods = _as_role_list(merged.get("discovery_methods"), "discovery_methods")
    legacy_method = _clean_str(merged.get("discovery_method"))
    if legacy_method:
        _dedup_extend(methods, legacy_method)
    _dedup_extend(methods, ACTIVITY_ATTRIBUTION)

    existing = merged.get("activity_attributions")
    if existing is None:
        entries = _synthesize_legacy_attributions(merged)
    elif not isinstance(existing, list) or any(
        not isinstance(item, dict) for item in existing
    ):
        raise ValueError(
            "raw_metadata.activity_attributions must be a list of dicts")
    else:
        entries = [dict(item) for item in existing]
    seen_ids = {item.get("event_id") for item in entries}
    if event_id not in seen_ids:
        entries.append(dict(attribution))

    merged["candidate_roles"] = roles
    merged["discovery_methods"] = methods
    merged["activity_attributions"] = entries
    return merged, merged != raw_metadata


def provenance_for_insert(
    attribution: dict[str, Any],
) -> dict[str, Any]:
    """Fresh provenance block for a first-time Activity insert.

    New list structure PLUS the legacy scalar activity_* mirror so older
    readers keep working; both encode the same facts and merge idempotently.
    """
    merged, changed = merge_activity_provenance({}, attribution)
    assert changed  # a fresh row always gains provenance
    for attr_key, scalar_key in _SCALAR_MIRROR.items():
        value = attribution.get(attr_key)
        if value is not None:
            merged[scalar_key] = value
    return merged


def candidate_roles(raw_metadata: dict[str, Any] | None) -> list[str]:
    """Interpret provenance roles: new list, else legacy scalar, else none."""
    if not isinstance(raw_metadata, dict):
        return []
    roles = raw_metadata.get("candidate_roles")
    if isinstance(roles, list):
        return [r for r in roles if isinstance(r, str)]
    scalar = _clean_str(raw_metadata.get("candidate_role"))
    return [scalar] if scalar else []


def discovery_methods(raw_metadata: dict[str, Any] | None) -> list[str]:
    """Interpret provenance methods: new list, else legacy scalar, else none."""
    if not isinstance(raw_metadata, dict):
        return []
    methods = raw_metadata.get("discovery_methods")
    if isinstance(methods, list):
        return [m for m in methods if isinstance(m, str)]
    scalar = _clean_str(raw_metadata.get("discovery_method"))
    return [scalar] if scalar else []
