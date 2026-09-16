"""Minimal Supabase REST adapter for Threads Insights and operator state."""

from __future__ import annotations

from datetime import datetime, timezone
import pathlib
import re
import time
from typing import Any

import httpx

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
    {"discovered", "reviewed", "approved", "rejected", "used", "stale"}
)

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
    "raw_metadata",
)
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Structured Threads pk shape: numeric id, optionally compound with : . _ -
# separators (e.g. "1788000000000001" or "555:1788000000000001").
_PK_RE = re.compile(r"^[0-9][0-9:._-]*$")


def trend_candidate_payload(
    account_key: str,
    url: str,
    *,
    now: str | None = None,
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
    """
    permalink, username = normalize_threads_post_url(url)
    if not account_key:
        raise ValueError("account_key is required for trend candidate writes")
    payload: dict[str, Any] = {
        "target_account_id": account_key,
        "source_platform": "threads",
        "source_permalink": permalink,
        "discovered_at": now or SupabaseStore._utc_now(),
        "raw_metadata": {
            "manual": True,
            "discovery_method": "manual_url",
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

    def update_trend_candidate_source_post_id(
        self, *, candidate_id: int, source_post_id: str
    ) -> dict[str, Any] | None:
        """Allowlisted factual UPDATE: writes ONLY ``source_post_id``.

        Account-scoped twice over: PATCH filters on id AND
        target_account_id, and the filtered row is returned for inspection.
        Deliberately cannot touch status, analysis fields, views, engagement
        counters, or raw_metadata — the body is constructed here from a
        single validated value. Fails closed on malformed input; returns
        None (no error) when the account-scoped filter matches no row.
        """
        account_key = self._require_account_key()
        if (
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id < 1
        ):
            raise ValueError("candidate id must be a positive integer")
        if not isinstance(source_post_id, str) or not source_post_id.strip():
            raise ValueError("source_post_id must be a non-blank string")
        pk = source_post_id.strip()
        if not _PK_RE.fullmatch(pk) or not any(ch.isdigit() for ch in pk):
            raise ValueError(
                "source_post_id must look like a structured pk "
                "(digits with optional :._- separators)"
            )
        response = self.client.patch(
            f"{self.base_url}/rest/v1/{TREND_CANDIDATES_TABLE}",
            headers={**self._headers, "Prefer": "return=representation"},
            params={
                "id": f"eq.{candidate_id}",
                "target_account_id": f"eq.{account_key}",
            },
            json={"source_post_id": source_post_id.strip()},
        )
        response.raise_for_status()
        rows = response.json() or []
        for row in rows:
            if isinstance(row, dict) and row.get("target_account_id") == account_key:
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
    ) -> dict[str, Any]:
        """Insert already-generated content as an account-scoped draft only."""
        account_key = self._require_account_key()
        if not isinstance(main_post_text, str) or not main_post_text.strip():
            raise ValueError("main_post_text must not be empty")
        replies = list(reply_texts or [])
        if any(not isinstance(reply, str) or not reply.strip() for reply in replies):
            raise ValueError("reply_texts must contain non-empty strings")

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
        params = {
            "account_key": f"eq.{account_key}",
            "status": "eq.approved",
            "scheduled_at": f"lte.{now or self._utc_now()}",
            "order": "scheduled_at.asc,id.asc",
            "limit": "1",
            "select": "*",
        }
        if campaign_code:
            params["campaign_code"] = f"eq.{campaign_code}"
        response = self.client.get(
            self._queue_url(table), headers=self._headers, params=params
        )
        response.raise_for_status()
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

    def mark_post_main_published(
        self, table: str, row_id: int | str, post_id: str
    ) -> None:
        self._patch_queue_row(
            table, row_id, {"threads_main_post_id": str(post_id)}
        )

    def mark_post_reply_progress(
        self, table: str, row_id: int | str, reply_ids: list[str]
    ) -> None:
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
        self._patch_queue_row(
            table,
            row_id,
            {
                "status": "posted",
                "threads_reply_ids": list(reply_ids),
                "posted_at": posted_at or self._utc_now(),
                "last_error": None,
            },
        )

    def mark_post_failed(
        self, table: str, row_id: int | str, error: str
    ) -> None:
        self._patch_queue_row(
            table,
            row_id,
            {"status": "failed", "last_error": str(error)[:2000]},
        )
