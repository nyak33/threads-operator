"""Minimal Supabase REST adapter for Threads Insights and operator state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pathlib
import re
import time
from typing import Any

import httpx

from .content_rules import validate_thread_texts
from .threads_api import POST_METRICS, has_usable_metrics
from .trend_urls import normalize_threads_post_url

# Supabase gateway blips (project-level latency spikes) return 502/503/504 on
# otherwise-valid GETs. Idempotent reads retry with exponential backoff so one
# blip does not abort a whole collector run; writes are NEVER retried blindly.
RETRY_STATUSES = frozenset({502, 503, 504})
MAX_GET_RETRIES = 3
GET_BACKOFF_BASE_SECONDS = 1.0


class _RetryGetTransport(httpx.BaseTransport):
    """Wrap an inner transport; retry only GETs on transient gateway errors."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        sleep=time.sleep,
        retries: int = MAX_GET_RETRIES,
        backoff_base: float = GET_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._inner = inner
        self._sleep = sleep
        self._retries = retries
        self._backoff_base = backoff_base

    def handle_request(
        self, request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        attempts = 0
        while True:
            response = self._inner.handle_request(request, *args, **kwargs)
            retryable = (
                request.method.upper() == "GET"
                and response.status_code in RETRY_STATUSES
                and attempts < self._retries
            )
            if not retryable:
                return response
            self._sleep(self._backoff_base * (2**attempts))
            attempts += 1

    def close(self) -> None:
        self._inner.close()


def make_default_client(
    inner: httpx.BaseTransport | None = None,
    *,
    sleep=time.sleep,
    timeout: float = 60.0,
) -> httpx.Client:
    """Build a client hardened against transient Supabase gateway 5xx on reads."""
    transport = _RetryGetTransport(inner or httpx.HTTPTransport(), sleep=sleep)
    return httpx.Client(transport=transport, timeout=timeout)


ACCOUNT_TABLE = "threads_account_insights_snapshots"
POST_TABLE = "threads_post_insights_snapshots"
ACTIVITY_EVENTS_TABLE = "threads_activity_events"
OWN_POSTS_TABLE = "threads_posts"
TREND_CANDIDATES_TABLE = "threads_trend_candidates"

# Statuses that exist in the live schema check — do not invent new ones.
TREND_STATUSES = frozenset(
    {
        # Legacy v1 statuses (kept valid so historical rows keep parsing).
        "discovered",
        "reviewed",
        "approved",
        "rejected",
        "used",
        "stale",
        # Workflow A lifecycle (migration 010): trend candidate -> original
        # own post, approval-gated, enqueued through the normal publisher.
        "drafted",
        "pending_approval",
        "approved",
        "queued",
        "skipped",
        "posted",
        "failed",
        # Workflow A backlog (migration 012): unapproved drafts kept durable.
        "backlog",
        "discarded",
    }
)

_APPROVAL_TIMEOUT_HOURS = 24

# Explicit SELECT column allowlist: evidence fields only. Analysis fields
# (topic/tone/score...) are intentionally not fetched or fabricated here;
# reads pass through whatever the row already contains via the CLI.
TREND_EVIDENCE_COLUMNS = (
    "id",
    "target_account_id",
    "source_platform",
    "source_post_id",
    "source_username",
    "source_permalink",
    "source_text",
    "published_at",
    "discovered_at",
    "last_checked_at",
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "status",
    "topic",
    "used_in_queue_id",
    "raw_metadata",
)
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Structured Threads pk shape: numeric id, optionally compound with : . _ -
# separators (e.g. "1788000000000001" or "555:1788000000000001").
_PK_RE = re.compile(r"^[0-9][0-9:._-]*$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,60}$")

# Only the deterministic publish queue carries the durable retry-state columns
# (attempt_count / last_attempt_at / next_retry_at, migration 008). Backoff-aware
# selection and retry-state persistence are scoped to this table so the other
# queue tables (engagement, activity, ...) — which lack these columns — are
# never queried or patched with them.
RETRY_STATE_TABLES = frozenset({"threads_publish_queue"})


def _supports_retry_state(table: str) -> bool:
    return table in RETRY_STATE_TABLES


def _iso_utc_timestamp(value: Any, field: str) -> str:
    """Strict tz-aware ISO-8601 timestamp; naive/relative values rejected."""
    import datetime as _dt

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank ISO-8601 timestamp string")
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware (UTC) timestamp: {value!r}")
    return parsed.astimezone(_dt.timezone.utc).isoformat()


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer, got {value!r}")
    return value


def _validated_pk(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source_post_id must be a non-blank string")
    if not _PK_RE.fullmatch(value.strip()) or not any(c.isdigit() for c in value):
        raise ValueError(
            "source_post_id must look like a structured pk "
            "(digits with optional :._- separators)"
        )
    return value.strip()


def _validated_username(value: Any) -> str:
    if not isinstance(value, str) or not _USERNAME_RE.fullmatch(value.strip()):
        raise ValueError("source_username must be a plain Threads username")
    return value.strip()


def _validated_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source_text must be a non-blank string")
    return value


_EVIDENCE_VALIDATORS: dict[str, Any] = {
    # The ONLY columns trend evidence enrichment may touch. Anything not
    # here (status, views, topic, analysis fields, raw_metadata,
    # target_account_id, ...) is rejected by the store method itself.
    "source_post_id": _validated_pk,
    "source_username": _validated_username,
    "source_text": _validated_text,
    "published_at": lambda v: _iso_utc_timestamp(v, "published_at"),
    "last_checked_at": lambda v: _iso_utc_timestamp(v, "last_checked_at"),
    "likes": lambda v: _non_negative_int(v, "likes"),
    "replies": lambda v: _non_negative_int(v, "replies"),
    "reposts": lambda v: _non_negative_int(v, "reposts"),
    "quotes": lambda v: _non_negative_int(v, "quotes"),
}


TREND_CANDIDATE_ROLES = {
    # Scalar channel FACTS recorded inside raw_metadata at insertion time.
    # These are NOT analytical roles. "manual_ingress" only records HOW the
    # row entered the store: manual ingestion alone does NOT imply
    # external_trend (that analytical role is reserved for a future
    # explicit external-discovery workflow). "external_trend" remains a
    # valid legacy value so historical rows keep parsing, but it is no
    # longer assigned automatically. The proven analytical role
    # own_performance comes from Activity attribution via the
    # trend_provenance merge layer (raw_metadata.candidate_roles).
    "external_trend": {"manual": True, "discovery_method": "manual_url"},
    "manual_ingress": {"manual": True, "discovery_method": "manual_url"},
    "own_performance": {"manual": False,
                        "discovery_method": "activity_attribution"},
}


def trend_candidate_payload(
    account_key: str,
    url: str,
    *,
    now: str | None = None,
    candidate_role: str = "manual_ingress",
    source_username: str | None = None,
    source_text: str | None = None,
    published_at: str | None = None,
    views: int | None = None,
    likes: int | None = None,
    replies: int | None = None,
    reposts: int | None = None,
    quotes: int | None = None,
    raw_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic insert payload for one trend candidate.

    Shared by the live store insert and the CLI dry-run preview so both
    render the exact same sanitized shape. Analysis fields (topic, tone,
    trend_score, ...) are intentionally never set here.

    candidate_role selects the raw_metadata scalar channel facts:
    manual_ingress (default) => manual=true/manual_url — how the row
    entered the store, with NO analytical role assigned (manual alone is
    not proof of external_trend); own_performance => manual=
    false/activity_attribution. external_trend remains accepted only for
    legacy-compatible callers. Unknown roles are rejected before any write.
    """
    permalink, username = normalize_threads_post_url(url)
    if not account_key:
        raise ValueError("account_key is required for trend candidate writes")
    if candidate_role not in TREND_CANDIDATE_ROLES:
        raise ValueError(
            "candidate_role must be one of: "
            + ", ".join(sorted(TREND_CANDIDATE_ROLES)))
    role_meta = TREND_CANDIDATE_ROLES[candidate_role]
    payload: dict[str, Any] = {
        "target_account_id": account_key,
        "source_platform": "threads",
        "source_permalink": permalink,
        "discovered_at": now or SupabaseStore._utc_now(),
        "raw_metadata": {
            "candidate_role": candidate_role,
            **role_meta,
            **(raw_metadata or {}),
        },
    }
    if username:
        payload["source_username"] = username
    if source_username:
        payload["source_username"] = source_username
    if source_text:
        payload["source_text"] = source_text
    if published_at:
        payload["published_at"] = published_at
    for name, value in (
        ("views", views),
        ("likes", likes),
        ("replies", replies),
        ("reposts", reposts),
        ("quotes", quotes),
    ):
        if value is not None:
            payload[name] = int(value)
    return payload


class SupabaseStore:
    def __init__(
        self,
        base_url: str,
        service_role_key: str,
        client: httpx.Client | None = None,
        account_key: str | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Supabase URL is required")
        if not service_role_key:
            raise ValueError("Supabase service role key is required")
        self.base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.account_key = account_key
        self.client = client or make_default_client()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }

    def insert_account_snapshot(self, payload: dict[str, Any]) -> None:
        self._insert(ACCOUNT_TABLE, payload)

    def insert_post_snapshot(self, payload: dict[str, Any]) -> None:
        self._insert(POST_TABLE, payload)

    def latest_post_snapshot(self, post_id: str) -> dict[str, Any] | None:
        return self._latest_snapshot(POST_TABLE, "post_id", post_id)

    def latest_post_snapshots(
        self, captured_since: str, *, page_size: int = 1000
    ) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        offset = 0
        select = "post_id,captured_at," + ",".join(POST_METRICS)
        while True:
            response = self.client.get(
                f"{self.base_url}/rest/v1/{POST_TABLE}",
                headers=self._headers,
                params={
                    "captured_at": f"gte.{captured_since}",
                    "order": "captured_at.desc",
                    "limit": str(page_size),
                    "offset": str(offset),
                    "select": select,
                },
            )
            response.raise_for_status()
            rows = response.json()
            for row in rows:
                post_id = row.get("post_id")
                if not post_id or post_id in latest:
                    continue
                if not has_usable_metrics(row):
                    continue
                latest[post_id] = row
            if len(rows) < page_size:
                break
            offset += page_size
        return latest

    def latest_account_snapshot(self, account_id: str) -> dict[str, Any] | None:
        return self._latest_snapshot(ACCOUNT_TABLE, "account_id", account_id)

    def _latest_snapshot(
        self, table: str, key: str, value: str
    ) -> dict[str, Any] | None:
        response = self.client.get(
            f"{self.base_url}/rest/v1/{table}",
            headers=self._headers,
            params={
                key: f"eq.{value}",
                "order": "captured_at.desc",
                "limit": "1",
                "select": "*",
            },
        )
        response.raise_for_status()
        rows = response.json()
        return None if not rows else rows[0]

    def _insert(self, table: str, payload: dict[str, Any]) -> None:
        headers = {**self._headers, "Prefer": "return=minimal"}
        response = self.client.post(
            f"{self.base_url}/rest/v1/{table}", headers=headers, json=payload
        )
        response.raise_for_status()

    def read_activity_follows_sync(
        self, profile_dir: pathlib.Path, settle_seconds: float = 3.0
    ) -> dict:
        from .activity_browser import read_activity_follows_sync

        return read_activity_follows_sync(profile_dir, settle_seconds=settle_seconds)

    def insert_activity_events(self, events: list[dict[str, Any]]) -> int:
        """Append only unseen Activity observation states for this account."""
        if not events:
            return 0
        fingerprints = [str(evt.get("fallback_fingerprint") or "") for evt in events]
        fingerprints = [fp for fp in fingerprints if fp]
        existing: set[str] = set()
        if fingerprints:
            params = {
                "fallback_fingerprint": f"in.({','.join(fingerprints)})",
                "select": "fallback_fingerprint",
            }
            if self.account_key:
                params["account_key"] = f"eq.{self.account_key}"
            response = self.client.get(
                f"{self.base_url}/rest/v1/{ACTIVITY_EVENTS_TABLE}",
                headers=self._headers,
                params=params,
            )
            response.raise_for_status()
            existing = {
                str(row.get("fallback_fingerprint")) for row in response.json()
            }

        new_payloads: list[dict[str, Any]] = []
        seen = set(existing)
        for evt in events:
            fingerprint = str(evt.get("fallback_fingerprint") or "")
            is_new = bool(fingerprint) and fingerprint not in seen
            evt["upserted"] = is_new
            if not is_new:
                continue
            seen.add(fingerprint)
            payload = {k: v for k, v in evt.items() if k != "upserted"}
            if self.account_key:
                payload["account_key"] = self.account_key
            new_payloads.append(payload)

        if not new_payloads:
            return 0
        headers = {**self._headers, "Prefer": "return=minimal"}
        response = self.client.post(
            f"{self.base_url}/rest/v1/{ACTIVITY_EVENTS_TABLE}",
            headers=headers,
            json=new_payloads,
        )
        response.raise_for_status()
        return len(new_payloads)

    def insert_trend_candidate(
        self,
        url: str,
        *,
        candidate_role: str = "manual_ingress",
        source_username: str | None = None,
        source_text: str | None = None,
        published_at: str | None = None,
        views: int | None = None,
        likes: int | None = None,
        replies: int | None = None,
        reposts: int | None = None,
        quotes: int | None = None,
        raw_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deterministically insert one public Threads post as a trend candidate.

        target_account_id is ALWAYS derived from self.account_key — callers
        cannot cross-write another account. Dedup uses the live unique
        identity (target_account_id, source_permalink); an existing row
        returns status="existing" with no second insert. source_post_id is
        left unset (NULL) — never fabricated from the permalink code.
        """
        permalink, _username = normalize_threads_post_url(url)
        account_key = self._require_account_key()

        def _lookup() -> dict[str, Any] | None:
            response = self.client.get(
                f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
                headers=self._headers,
                params={
                    "target_account_id": f"eq.{account_key}",
                    "source_permalink": f"eq.{permalink}",
                    "select": "id,status",
                    "limit": "1",
                },
            )
            response.raise_for_status()
            rows = response.json() or []
            return rows[0] if rows else None

        found = _lookup()
        if found:
            return {
                "status": "existing",
                "id": found.get("id"),
                "permalink": permalink,
            }
        payload = trend_candidate_payload(
            account_key,
            url,
            candidate_role=candidate_role,
            source_username=source_username,
            source_text=source_text,
            published_at=published_at,
            views=views,
            likes=likes,
            replies=replies,
            reposts=reposts,
            quotes=quotes,
            raw_metadata=raw_metadata,
        )

        headers = {**self._headers, "Prefer": "return=representation"}
        response = self.client.post(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers=headers,
            json=payload,
        )
        if response.status_code == 409:
            # Lost a race against an identical insert: the row exists now.
            found = _lookup()
            return {
                "status": "existing",
                "id": (found or {}).get("id"),
                "permalink": permalink,
            }
        response.raise_for_status()
        rows = response.json() or []
        return {
            "status": "inserted",
            "id": rows[0].get("id") if rows else None,
            "permalink": permalink,
        }

    # ------------------------------------------------------------------ reads

    def list_trend_candidates(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """SELECT-only, account-scoped trend candidate listing.

        Account isolation is enforced here: the filter always uses
        self.account_key and callers cannot pass another identity. Newest
        discovered_at first. No writes, no Threads API, no browser, no LLM.
        """
        account_key = self._require_account_key()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if status is not None and status not in TREND_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(TREND_STATUSES))
            )
        params: dict[str, Any] = {
            "select": ",".join(TREND_EVIDENCE_COLUMNS),
            "target_account_id": f"eq.{account_key}",
            "order": "discovered_at.desc",
            "limit": str(limit),
        }
        if status:
            params["status"] = f"eq.{status}"
        response = self.client.get(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers=self._headers,
            params=params,
        )
        response.raise_for_status()
        rows = response.json() or []
        return [r for r in rows if isinstance(r, dict)]

    def get_trend_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """SELECT one candidate scoped by BOTH id and target_account_id.

        A row belonging to another account reads as missing (KeyError never
        escapes; returns None). Client-side account check is kept as
        belt-and-braces against a leaky server-side filter.
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        response = self.client.get(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers=self._headers,
            params={
                "select": ",".join(TREND_EVIDENCE_COLUMNS),
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
            },
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
                return row
        return None

    def update_trend_candidate_evidence(
        self,
        *,
        candidate_id: int,
        evidence: dict[str, Any],
        enrichment: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Account-scoped allowlisted factual UPDATE of observed evidence.

        The method itself enforces ``id == candidate_id AND
        target_account_id == account_key`` on both the merge read and the
        PATCH — callers cannot pass another identity. A strict allowlist
        gates every key (status, views, topic, trend_score, analysis
        fields, raw_metadata, permalink, account id are REJECTED here before
        any HTTP call), and every value is validated for shape. Absent keys
        are simply not in the body — this method can never null a field.

        ``enrichment`` facts (e.g. enrichment_source, media_type) are merged
        over raw_metadata: the existing object is read back account-scoped
        first and preserved; None raw_metadata merges into {}. A non-dict
        raw_metadata fails closed without writing. Returns the updated row
        or None when the account-scoped filter matches nothing (a
        foreign-account candidate behaves as not found and is never
        modified).
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("evidence must be a non-empty dict")

        body: dict[str, Any] = {}
        for key, value in evidence.items():
            validator = _EVIDENCE_VALIDATORS.get(key)
            if validator is None:
                raise ValueError(
                    f"evidence field {key!r} is not in the allowlist"
                )
            body[key] = validator(value)

        if enrichment is not None:
            if not isinstance(enrichment, dict) or not enrichment:
                raise ValueError("enrichment must be a non-empty dict when given")
            existing = self.get_trend_candidate(candidate_id)
            if existing is None:
                return None  # not found for THIS account: never modified
            current_raw = existing.get("raw_metadata")
            if current_raw is None:
                current_raw = {}
            if not isinstance(current_raw, dict):
                raise ValueError(
                    "existing raw_metadata is not an object — refusing to overwrite"
                )
            merged = {**current_raw, **enrichment}
            body["raw_metadata"] = merged

        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
            },
            json=body,
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
                return row
        return None

    def update_trend_candidate_source_post_id(
        self, *, candidate_id: int, source_post_id: str
    ) -> dict[str, Any] | None:
        """Legacy v1 single-field path — delegates to the v2 evidence method.

        One enforcement point: allowlist, value validation, account-scoped
        WHERE id AND target_account_id, and never-null semantics all live in
        update_trend_candidate_evidence.
        """
        return self.update_trend_candidate_evidence(
            candidate_id=candidate_id,
            evidence={"source_post_id": source_post_id},
        )

    # ------------------------------------------------------------- provenance

    def find_trend_candidate_by_permalink(
        self,
        permalink: str,
        *,
        account_key: str | None = None,
    ) -> dict[str, Any] | None:
        """SELECT one candidate by the unique (account, permalink) identity.

        ``account_key`` defaults to the store's own account and is ALWAYS
        part of the filter — callers cannot probe another account. Returns
        the full evidence-shaped row (id, source_username, raw_metadata,
        ...) or None. Read-only.
        """
        effective = account_key or self._require_account_key()
        if not effective:
            raise ValueError("account_key is required for candidate reads")
        response = self.client.get(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers=self._headers,
            params={
                "target_account_id": f"eq.{effective}",
                "source_permalink": f"eq.{permalink}",
                "select": ",".join(TREND_EVIDENCE_COLUMNS),
                "limit": "1",
            },
        )
        response.raise_for_status()
        for row in response.json() or []:
            if isinstance(row, dict) and row.get("target_account_id") == effective:
                return row
        return None

    def update_trend_candidate_provenance(
        self,
        *,
        candidate_id: int,
        raw_metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Account-scoped raw_metadata-ONLY provenance UPDATE.

        The blast radius is structurally one column: the request body is
        exactly ``{"raw_metadata": ...}`` — status, source_text,
        source_post_id, metrics, views, trend_score, topic, hook_type,
        tone, why_it_works and adaptation_angle CANNOT be modified through
        this path. WHERE is always ``id == candidate_id AND
        target_account_id == self.account_key``; a foreign-account row
        matches nothing and returns None without any write attempt on it.
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        if not isinstance(raw_metadata, dict) or not raw_metadata:
            raise ValueError("raw_metadata must be a non-empty dict")
        if not isinstance(raw_metadata.get("candidate_roles"), list) and \
                not isinstance(raw_metadata.get("discovery_methods"), list) and \
                not isinstance(raw_metadata.get("activity_attributions"), list):
            raise ValueError(
                "provenance update requires at least one provenance list")

        # Read back account-scoped first so a non-dict raw_metadata or a
        # missing/foreign row can never be clobbered blindly.
        existing = self.get_trend_candidate(candidate_id)
        if existing is None:
            return None
        current_raw = existing.get("raw_metadata")
        if current_raw is not None and not isinstance(current_raw, dict):
            raise ValueError(
                "existing raw_metadata is not an object — refusing to overwrite")

        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
            },
            json={"raw_metadata": raw_metadata},
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
                return row
        return None

    # ------------------------------------------------- Workflow A: trend drafts

    def update_trend_candidate_workflow_a(
        self,
        *,
        candidate_id: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Account-scoped UPDATE for the Workflow A approval lifecycle.

        Dedicated to status/draft transitions (drafted, pending_approval,
        approved, queued, rejected, skipped, posted, failed) plus the draft
        payload carried in raw_metadata.workflow_a. Unlike the evidence
        method, this path IS allowed to set status/topic/trend_score/
        adaptation_angle/why_it_works/used_in_queue_id — and nothing else.
        WHERE is always ``id == candidate_id AND target_account_id ==
        self.account_key``.
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields must be a non-empty dict")

        allowed = {
            "status",
            "topic",
            "trend_score",
            "adaptation_angle",
            "why_it_works",
            "used_in_queue_id",
            "approval_sent_at",
            "timed_out_at",
            "raw_metadata",
        }
        body: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"workflow_a field {key!r} is not in the allowlist")
            if key == "status":
                if value not in TREND_STATUSES:
                    raise ValueError(f"invalid trend status {value!r}")
                body[key] = value
            elif key == "raw_metadata":
                if not isinstance(value, dict):
                    raise ValueError("raw_metadata must be an object")
                body[key] = value
            elif key == "used_in_queue_id":
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                ):
                    raise ValueError("used_in_queue_id must be an integer or None")
                body[key] = value
            elif key == "trend_score":
                body[key] = None if value is None else float(value)
            else:
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{key} must be a string or None")
                body[key] = value

        # raw_metadata merges over the existing object (read account-scoped
        # first) so a draft write cannot clobber provenance.
        if "raw_metadata" in body:
            existing = self.get_trend_candidate(candidate_id)
            if existing is None:
                return None
            current_raw = existing.get("raw_metadata")
            if current_raw is not None and not isinstance(current_raw, dict):
                raise ValueError(
                    "existing raw_metadata is not an object — refusing to overwrite"
                )
            body["raw_metadata"] = {**(current_raw or {}), **body["raw_metadata"]}

        body["updated_at"] = self._utc_now()
        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
            },
            json=body,
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
                return row
        return None

    def transition_trend_candidate(
        self,
        *,
        candidate_id: int,
        from_status: str,
        to_status: str,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS status transition — only fires when the row is still in
        ``from_status``. Returns the updated row, or None when the
        precondition no longer holds (another worker already moved it)."""
        account_key = self._require_account_key()
        if to_status not in TREND_STATUSES:
            raise ValueError(f"invalid trend status {to_status!r}")
        body: dict[str, Any] = {"status": to_status, "updated_at": self._utc_now()}
        for key, value in (fields or {}).items():
            if key == "status":
                continue
            body[key] = value
        if "raw_metadata" in body:
            existing = self.get_trend_candidate(candidate_id)
            if existing is None:
                return None
            current_raw = existing.get("raw_metadata")
            if current_raw is not None and not isinstance(current_raw, dict):
                raise ValueError(
                    "existing raw_metadata is not an object — refusing to overwrite"
                )
            body["raw_metadata"] = {**(current_raw or {}), **body["raw_metadata"]}
        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
                "status": f"eq.{from_status}",
            },
            json=body,
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
                return row
        return None

    # ------------------------------------------------- Workflow A backlog

    def list_trend_backlog(
        self,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """SELECT unused backlog items for this account, newest timed_out first.

        Only returns rows whose ``status = 'backlog'`` and
        ``used_in_queue_id IS NULL`` (never enqueued). Account isolation is
        enforced via ``self.account_key`` — callers cannot probe another
        identity. No writes, no LLM, no browser.
        """
        account_key = self._require_account_key()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        params: dict[str, Any] = {
            "select": ",".join(TREND_EVIDENCE_COLUMNS),
            "target_account_id": f"eq.{account_key}",
            "status": "eq.backlog",
            "used_in_queue_id": "is.null",
            "order": "timed_out_at.desc",
            "limit": str(limit),
        }
        response = self.client.get(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers=self._headers,
            params=params,
        )
        response.raise_for_status()
        return [r for r in response.json() or [] if isinstance(r, dict)]

    def transition_trend_candidate_backlog(
        self,
        *,
        candidate_id: int,
        timed_out_at: str | None = None,
    ) -> dict[str, Any] | None:
        """CAS ``pending_approval`` -> ``backlog`` for one row.

        Only fires when the row is still ``pending_approval``. Any other
        status (approved/queued/rejected/skipped/posted/failed/backlog)
        returns None. Idempotent by construction: a second invocation on the
        same row sees ``status != pending_approval`` and does nothing.
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        ts = timed_out_at or self._utc_now()
        body: dict[str, Any] = {
            "status": "backlog",
            "timed_out_at": ts,
            "updated_at": self._utc_now(),
        }
        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
                "status": "eq.pending_approval",
            },
            json=body,
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
                return row
        return None

    def mark_trend_candidate_approval_sent(
        self,
        *,
        candidate_id: int,
        sent_at: str | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Record confirmed Telegram delivery for one row.

        Sets ``approval_sent_at`` and persists the Telegram message reference
        (``"<chat_id>:<message_id>"``) in ``raw_metadata.workflow_a`` under
        ``approval_ref``/``approval_message`` so approval cards can be
        correlated with their Telegram message.

        Idempotent claim: only writes when the row is ``pending_approval`` or
        ``drafted`` AND ``approval_sent_at IS NULL``. A second invocation
        (repeat watchdog run, concurrent worker) sees a non-null
        ``approval_sent_at`` and does nothing — this is the DB-side
        duplicate-card guard. The draft flow claims while still ``drafted``
        (claim-before-send); the legacy recovery flow claims while
        ``pending_approval``.

        Returns the updated row, or None when the CAS did not apply.
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        ts = sent_at or self._utc_now()
        body: dict[str, Any] = {
            "approval_sent_at": ts,
            "updated_at": self._utc_now(),
        }
        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
                "status": "in.(pending_approval,drafted)",
                "approval_sent_at": "is.null",
            },
            json=body,
        )
        response.raise_for_status()
        rows = response.json() or []
        row: dict[str, Any] | None = None
        for r in rows:
            if isinstance(r, dict) and r.get("target_account_id") == account_key:
                row = r
                break
        if row is None:
            return None
        if approval_ref:
            self._merge_trend_candidate_approval_ref(
                candidate_id=candidate_id,
                account_key=account_key,
                approval_ref=approval_ref,
            )
        return row

    def _merge_trend_candidate_approval_ref(
        self,
        *,
        candidate_id: int,
        account_key: str,
        approval_ref: str,
    ) -> None:
        """Best-effort merge of the Telegram message ref into raw_metadata."""
        now = self._utc_now()
        for _ in range(3):
            response = self.client.get(
                f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
                headers=self._headers,
                params={
                    "select": "id,status,approval_sent_at,raw_metadata",
                    "id": f"eq.{candidate_id}",
                    "target_account_id": f"eq.{account_key}",
                },
            )
            response.raise_for_status()
            rows = response.json() or []
            current = next((r for r in rows if isinstance(r, dict)), None)
            if current is None:
                return
            if current.get("status") != "pending_approval":
                return
            raw_metadata = dict(current.get("raw_metadata") or {})
            wa = dict(raw_metadata.get("workflow_a") or {})
            if wa.get("approval_ref") == approval_ref:
                return
            wa["approval_ref"] = approval_ref
            wa["approval_message"] = approval_ref
            raw_metadata["workflow_a"] = wa
            patch = self.client.patch(
                f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
                headers=self._headers,
                params={
                    "id": f"eq.{candidate_id}",
                    "target_account_id": f"eq.{account_key}",
                    "status": "eq.pending_approval",
                },
                json={"raw_metadata": raw_metadata, "updated_at": now},
            )
            if patch.status_code in (200, 204):
                return
            patch.raise_for_status()

    def find_stale_pending_approvals(
        self,
        *,
        older_than_hours: int = _APPROVAL_TIMEOUT_HOURS,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """SELECT pending_approval rows whose approval card was sent > cutoff.

        STRICT RULE: a row is only stale when ``approval_sent_at IS NOT NULL``
        and older than the cutoff. Rows with ``approval_sent_at IS NULL`` have
        never had a confirmed Telegram delivery, so they must NEVER time out
        into backlog (the old ``updated_at`` fallback moved undelivered rows
        and was removed on purpose).

        The caller must CAS each row through
        :meth:`transition_trend_candidate_backlog` before treating it as
        timed out — this read is only a hint, not the actual transition.
        """
        account_key = self._require_account_key()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        ).isoformat()
        params: dict[str, Any] = {
            "select": "id,status,approval_sent_at,updated_at",
            "target_account_id": f"eq.{account_key}",
            "status": "eq.pending_approval",
            "approval_sent_at": f"lt.{cutoff}",
            "order": "approval_sent_at.asc",
            "limit": str(limit),
        }
        response = self.client.get(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers=self._headers,
            params=params,
        )
        response.raise_for_status()
        return [r for r in response.json() or [] if isinstance(r, dict)]

    # --------------------------------------- Workflow B: own-post reply states

    OWN_REPLY_TABLE = "threads_own_reply_engagement"
    OWN_REPLY_STATUSES = frozenset(
        {
            "discovered",
            "pending_approval",
            "approved",
            "rejected",
            "ignored",
            "posting",
            "posted",
            "failed",
        }
    )

    def upsert_own_reply(self, *, reply: dict[str, Any]) -> dict[str, Any]:
        """Insert a discovered inbound reply, deduped by (account, reply_id).

        A reply that already exists returns the existing row unchanged —
        discovery is idempotent no matter how many watchdog ticks observe
        the same inbound reply. Only unhandled rows resurface.
        """
        account_key = self._require_account_key()
        reply_id = reply.get("reply_id")
        if not isinstance(reply_id, str) or not reply_id.strip():
            raise ValueError("reply_id must be a non-blank string")
        existing = self.get_own_reply_by_reply_id(reply_id.strip())
        if existing is not None:
            return existing

        parent_post_id = reply.get("parent_post_id")
        if not isinstance(parent_post_id, str) or not parent_post_id.strip():
            raise ValueError("parent_post_id must be a non-blank string")
        payload: dict[str, Any] = {
            "account_key": account_key,
            "reply_id": reply_id.strip(),
            "parent_post_id": parent_post_id.strip(),
            "status": "discovered",
        }
        for key in (
            "parent_post_permalink",
            "parent_post_text",
            "from_username",
            "reply_text",
            "reply_permalink",
            "replied_at",
        ):
            value = reply.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a string or None")
                payload[key] = value

        headers = {**self._headers, "Prefer": "return=representation"}
        response = self.client.post(
            f"{self.base_url}/rest/v1/{self.OWN_REPLY_TABLE}",
            headers=headers,
            json=payload,
        )
        if response.status_code == 409:
            raced = self.get_own_reply_by_reply_id(reply_id.strip())
            if raced is not None:
                return raced
        response.raise_for_status()
        rows = response.json() or []
        if not rows:
            raise ValueError("own-reply insert did not return a row")
        return rows[0]

    def get_own_reply_by_reply_id(self, reply_id: str) -> dict[str, Any] | None:
        account_key = self._require_account_key()
        response = self.client.get(
            f"{self.base_url}/rest/v1/{self.OWN_REPLY_TABLE}",
            headers=self._headers,
            params={
                "select": "*",
                "account_key": f"eq.{account_key}",
                "reply_id": f"eq.{reply_id}",
                "limit": "1",
            },
        )
        response.raise_for_status()
        for row in response.json() or []:
            if isinstance(row, dict) and row.get("account_key") == account_key:
                return row
        return None

    def get_own_reply(self, row_id: int) -> dict[str, Any] | None:
        account_key = self._require_account_key()
        response = self.client.get(
            f"{self.base_url}/rest/v1/{self.OWN_REPLY_TABLE}",
            headers=self._headers,
            params={
                "select": "*",
                "id": f"eq.{row_id}",
                "account_key": f"eq.{account_key}",
                "limit": "1",
            },
        )
        response.raise_for_status()
        for row in response.json() or []:
            if isinstance(row, dict) and row.get("account_key") == account_key:
                return row
        return None

    def list_own_replies(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        account_key = self._require_account_key()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        if status is not None and status not in self.OWN_REPLY_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(self.OWN_REPLY_STATUSES))
            )
        params: dict[str, Any] = {
            "select": "*",
            "account_key": f"eq.{account_key}",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        if status:
            params["status"] = f"eq.{status}"
        response = self.client.get(
            f"{self.base_url}/rest/v1/{self.OWN_REPLY_TABLE}",
            headers=self._headers,
            params=params,
        )
        response.raise_for_status()
        return [r for r in response.json() or [] if isinstance(r, dict)]

    def transition_own_reply(
        self,
        *,
        row_id: int,
        from_status: str | tuple[str, ...],
        to_status: str,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS transition on a Workflow B row. ``from_status`` may be a tuple
        for transitions legal from several states (e.g. reject from
        discovered OR pending_approval). None = precondition failed."""
        account_key = self._require_account_key()
        if to_status not in self.OWN_REPLY_STATUSES:
            raise ValueError(f"invalid own-reply status {to_status!r}")
        body: dict[str, Any] = {"status": to_status, "updated_at": self._utc_now()}
        for key, value in (fields or {}).items():
            if key in {"id", "account_key", "reply_id", "status"}:
                continue
            body[key] = value
        from_list = (
            [from_status] if isinstance(from_status, str) else list(from_status)
        )
        for state in from_list:
            if state not in self.OWN_REPLY_STATUSES:
                raise ValueError(f"invalid from_status {state!r}")
        params: dict[str, Any] = {
            "id": f"eq.{row_id}",
            "account_key": f"eq.{account_key}",
        }
        if len(from_list) == 1:
            params["status"] = f"eq.{from_list[0]}"
        else:
            params["status"] = "in.(" + ",".join(from_list) + ")"
        response = self.client.patch(
            f"{self.base_url}/rest/v1/{self.OWN_REPLY_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params=params,
            json=body,
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("account_key") == account_key:
                return row
        return None


    def list_posts(self) -> list[dict[str, Any]]:
        response = self.client.get(
            f"{self.base_url}/rest/v1/{OWN_POSTS_TABLE}",
            headers=self._headers,
            params={"select": "thread_id,text,published_at,permalink"},
        )
        response.raise_for_status()
        return response.json() or []

    def _queue_url(self, table: str) -> str:
        if not _TABLE_RE.fullmatch(table):
            raise ValueError("Invalid Supabase table name")
        return f"{self.base_url}/rest/v1/{table}"

    def _require_account_key(self) -> str:
        if not self.account_key:
            raise ValueError("account_key is required for account-scoped queue operations")
        return self.account_key

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def enqueue_draft(
        self,
        table: str,
        main_post_text: str,
        reply_texts: list[str] | None = None,
        campaign_code: str | None = None,
        scheduled_at: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """Insert already-generated content as an account-scoped draft only.

        ``topic`` (when given) is written to the queue's topic column so it
        carries through to the published post's Meta ``topic_tag``.
        """
        account_key = self._require_account_key()
        if not isinstance(main_post_text, str) or not main_post_text.strip():
            raise ValueError("main_post_text must not be empty")
        replies = list(reply_texts or [])
        if any(not isinstance(reply, str) or not reply.strip() for reply in replies):
            raise ValueError("reply_texts must contain non-empty strings")
        validate_thread_texts(main_post_text, replies)

        payload: dict[str, Any] = {
            "account_key": account_key,
            "main_post_text": main_post_text,
            "reply_texts": replies,
            "status": "draft",
        }
        if campaign_code:
            payload["campaign_code"] = campaign_code
        if scheduled_at:
            payload["scheduled_at"] = scheduled_at
        if topic and topic.strip():
            payload["topic"] = topic.strip()

        headers = {**self._headers, "Prefer": "return=representation"}
        response = self.client.post(
            self._queue_url(table),
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        rows = response.json() or []
        if not rows:
            raise ValueError("Draft insert did not return a queue row")
        return rows[0]

    def peek_due_post(
        self,
        table: str,
        campaign_code: str | None = None,
        *,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        account_key = self._require_account_key()
        now_ts = now or self._utc_now()
        params = {
            "account_key": f"eq.{account_key}",
            "status": "eq.approved",
            "scheduled_at": f"lte.{now_ts}",
            "order": "scheduled_at.asc,id.asc",
            "limit": "1",
            "select": "*",
        }
        if campaign_code:
            params["campaign_code"] = f"eq.{campaign_code}"
        if _supports_retry_state(table):
            # Skip rows currently in retry backoff. next_retry_at IS NULL means
            # "not in backoff" (never attempted, or backoff cleared). A future
            # next_retry_at means the row is waiting out a persisted backoff and
            # must not be re-selected — this is what stops a repeatedly failing
            # row from starving every later due row.
            params["or"] = f"(next_retry_at.is.null,next_retry_at.lte.{now_ts})"
        response = self.client.get(
            self._queue_url(table), headers=self._headers, params=params
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Migration 008 not yet applied: the retry columns do not exist, so
            # PostgREST rejects the next_retry_at filter with a 400. Fall back
            # to plain oldest-due selection (pre-fix behaviour) rather than
            # hard-crashing the worker — the durable fix activates once the
            # columns are added.
            if "or" in params and exc.response is not None and exc.response.status_code == 400:
                params.pop("or", None)
                response = self.client.get(
                    self._queue_url(table), headers=self._headers, params=params
                )
                response.raise_for_status()
            else:
                raise
        rows = response.json() or []
        return rows[0] if rows else None

    def claim_due_post(
        self,
        table: str,
        campaign_code: str | None = None,
        *,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        account_key = self._require_account_key()
        claimed_at = now or self._utc_now()
        candidate = self.peek_due_post(
            table, campaign_code=campaign_code, now=claimed_at
        )
        if not candidate:
            return None
        row_id = candidate.get("id")
        if row_id is None:
            raise ValueError("Queue row is missing id")
        headers = {**self._headers, "Prefer": "return=representation"}
        claim_payload = {"status": "posting", "claimed_at": claimed_at}
        if _supports_retry_state(table):
            claim_payload = {**claim_payload, "heartbeat_at": claimed_at}
        response = self.client.patch(
            self._queue_url(table),
            headers=headers,
            params={
                "id": f"eq.{row_id}",
                "account_key": f"eq.{account_key}",
                "status": "eq.approved",
            },
            json=claim_payload,
        )
        if response.status_code == 400 and "heartbeat_at" in claim_payload:
            # Migration 009 not applied: drop the heartbeat field and retry the
            # identical CAS claim (a genuine claim race returns 200 + [], not
            # 400, so this fallback cannot mask a lost race).
            response = self.client.patch(
                self._queue_url(table),
                headers=headers,
                params={
                    "id": f"eq.{row_id}",
                    "account_key": f"eq.{account_key}",
                    "status": "eq.approved",
                },
                json={"status": "posting", "claimed_at": claimed_at},
            )
        response.raise_for_status()
        rows = response.json() or []
        return rows[0] if rows else None

    def _patch_queue_row(
        self, table: str, row_id: int | str, payload: dict[str, Any]
    ) -> None:
        account_key = self._require_account_key()
        headers = {**self._headers, "Prefer": "return=minimal"}
        response = self.client.patch(
            self._queue_url(table),
            headers=headers,
            params={"id": f"eq.{row_id}", "account_key": f"eq.{account_key}"},
            json=payload,
        )
        response.raise_for_status()

    def _patch_queue_row_with_heartbeat(
        self, table: str, row_id: int | str, payload: dict[str, Any]
    ) -> None:
        """PATCH the row plus a fresh ``heartbeat_at`` in one write.

        Every durable progress checkpoint (claim, root persisted, reply
        persisted, completion) doubles as a heartbeat, so a live worker is
        always distinguishable from a crashed one without extra API calls.
        Degrades gracefully when migration 009 has not been applied: the first
        attempt 400s on the unknown column, the retry drops it.
        """
        account_key = self._require_account_key()
        headers = {**self._headers, "Prefer": "return=minimal"}
        params = {"id": f"eq.{row_id}", "account_key": f"eq.{account_key}"}
        stamped = {**payload, "heartbeat_at": self._utc_now()}
        response = self.client.patch(
            self._queue_url(table), headers=headers, params=params, json=stamped
        )
        if response.status_code == 400 and "heartbeat_at" in str(response.text):
            response = self.client.patch(
                self._queue_url(table), headers=headers, params=params, json=payload
            )
        response.raise_for_status()

    def touch_post_heartbeat(self, table: str, row_id: int | str) -> bool:
        """Advance ``heartbeat_at`` on a row still being published.

        Called on claim, root publish/persist, every reply publish/persist and
        completion, so any observer can tell a live worker from a dead one.
        Returns True when the heartbeat was written. Tables without the
        heartbeat column (migration 009 not applied) degrade to False so the
        worker keeps running on claimed_at-based staleness instead.
        """
        if not _supports_retry_state(table):
            return False
        try:
            self._patch_queue_row_with_heartbeat(table, row_id, {})
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                return False
            raise

    def reclaim_stale_posting_rows(
        self,
        table: str,
        *,
        stale_seconds: float = 600.0,
        limit: int = 3,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Safely re-claim ``posting`` rows whose worker died mid-thread.

        A row stays ``posting`` (no status flip, so no second worker can pick it
        up through the approved path). The claim is won with a CAS PATCH: the
        update only lands while the row is still ``posting`` AND its last
        heartbeat (falling back to claimed_at, then updated_at when the
        heartbeat column is absent) is older than ``stale_seconds``. The winner
        rewrites claimed_at + heartbeat_at atomically; losers get zero rows.
        Resumability comes from the persisted checkpoint (main id + reply ids) —
        the resuming worker reconciles live state before publishing anything.
        """
        account_key = self._require_account_key()
        now_dt = datetime.fromisoformat((now or self._utc_now()).replace("Z", "+00:00"))
        cutoff = (now_dt - timedelta(seconds=stale_seconds)).isoformat()
        headers = {**self._headers, "Prefer": "return=representation"}
        # Prefer the heartbeat column; degrade to claimed_at when migration 009
        # has not been applied yet (PostgREST 400 on unknown column).
        filter_sets = [
            {"or": f"(heartbeat_at.lt.{cutoff},and(heartbeat_at.is.null,claimed_at.lt.{cutoff}))"},
            {"claimed_at": f"lt.{cutoff}"},
        ]
        rows: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        extra: dict[str, str] = filter_sets[-1]
        for candidate_filter in filter_sets:
            extra = candidate_filter
            params = {
                "account_key": f"eq.{account_key}",
                "status": "eq.posting",
                "order": "claimed_at.asc,id.asc",
                "limit": str(limit),
                "select": "id",
                **extra,
            }
            response = self.client.get(
                self._queue_url(table), headers=self._headers, params=params
            )
            if response.status_code == 400 and extra is filter_sets[0]:
                continue
            response.raise_for_status()
            candidates = response.json() or []
            break
        for cand in candidates:
            row_id = cand.get("id")
            if row_id is None:
                continue
            response = self.client.patch(
                self._queue_url(table),
                headers=headers,
                params={
                    "id": f"eq.{row_id}",
                    "account_key": f"eq.{account_key}",
                    "status": "eq.posting",
                    **extra,
                },
                json={"claimed_at": now_dt.isoformat(), "heartbeat_at": now_dt.isoformat()},
            )
            if response.status_code == 400:
                # heartbeat column missing — retry CAS on claimed_at only.
                response = self.client.patch(
                    self._queue_url(table),
                    headers=headers,
                    params={
                        "id": f"eq.{row_id}",
                        "account_key": f"eq.{account_key}",
                        "status": "eq.posting",
                        "claimed_at": f"lt.{cutoff}",
                    },
                    json={"claimed_at": now_dt.isoformat()},
                )
            response.raise_for_status()
            won = response.json() or []
            if won:
                full = self.fetch_queue_row(table, row_id)
                if full:
                    rows.append(full)
        return rows

    def mark_post_main_published(
        self, table: str, row_id: int | str, post_id: str
    ) -> None:
        if _supports_retry_state(table):
            self._patch_queue_row_with_heartbeat(
                table, row_id, {"threads_main_post_id": str(post_id)}
            )
            return
        self._patch_queue_row(
            table, row_id, {"threads_main_post_id": str(post_id)}
        )

    def mark_post_reply_progress(
        self, table: str, row_id: int | str, reply_ids: list[str]
    ) -> None:
        if _supports_retry_state(table):
            # The single most important checkpoint: after EVERY successful
            # reply publish, the full ordered reply-id list plus a heartbeat
            # land in one atomic write, so a crash at any later point resumes
            # from exactly here with zero root/reply duplication.
            self._patch_queue_row_with_heartbeat(
                table, row_id, {"threads_reply_ids": list(reply_ids)}
            )
            return
        self._patch_queue_row(
            table, row_id, {"threads_reply_ids": list(reply_ids)}
        )

    def mark_post_posted(
        self,
        table: str,
        row_id: int | str,
        reply_ids: list[str],
        *,
        posted_at: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": "posted",
            "threads_reply_ids": list(reply_ids),
            "posted_at": posted_at or self._utc_now(),
            "last_error": None,
        }
        if _supports_retry_state(table):
            # Success: clear the retry state so the row is terminal and clean.
            payload["next_retry_at"] = None
            self._patch_queue_row_with_heartbeat(table, row_id, payload)
            return
        self._patch_queue_row(table, row_id, payload)

    def mark_post_failed(
        self,
        table: str,
        row_id: int | str,
        error: str,
        *,
        clear_next_retry: bool = True,
    ) -> None:
        payload: dict[str, Any] = {"status": "failed", "last_error": str(error)[:2000]}
        if _supports_retry_state(table) and clear_next_retry:
            # Terminal failure: stop any further retry scheduling. A row that is
            # manually requeued (status -> approved) resets next_retry_at to NULL
            # via requeue paths so it becomes eligible again.
            payload["next_retry_at"] = None
        self._patch_queue_row(table, row_id, payload)

    def record_publish_attempt(
        self,
        table: str,
        row_id: int | str,
        *,
        attempt_count: int,
        attempted_at: str | None = None,
        next_retry_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Persist retry state for a row without changing its status.

        Used by the recovery worker to stamp the durable attempt counter and the
        backoff gate (``next_retry_at``) after every publish attempt. Only
        applies to tables that carry the retry-state columns; a no-op otherwise.
        """
        if not _supports_retry_state(table):
            return
        payload: dict[str, Any] = {
            "attempt_count": int(attempt_count),
            "last_attempt_at": attempted_at or self._utc_now(),
            "next_retry_at": next_retry_at,
        }
        if last_error is not None:
            payload["last_error"] = str(last_error)[:2000]
        self._patch_queue_row(table, row_id, payload)

    def fetch_queue_row(self, table: str, row_id: int | str) -> dict[str, Any] | None:
        account_key = self._require_account_key()
        response = self.client.get(
            self._queue_url(table),
            headers=self._headers,
            params={"id": f"eq.{row_id}", "account_key": f"eq.{account_key}", "select": "*"},
        )
        response.raise_for_status()
        rows = response.json() or []
        return rows[0] if rows else None
