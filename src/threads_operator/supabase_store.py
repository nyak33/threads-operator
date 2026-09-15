"""Minimal Supabase REST adapter for Threads Insights and operator state."""

from __future__ import annotations

from datetime import datetime, timezone
import pathlib
import re
from typing import Any

import httpx

from .threads_api import POST_METRICS, has_usable_metrics

ACCOUNT_TABLE = "threads_account_insights_snapshots"
POST_TABLE = "threads_post_insights_snapshots"
ACTIVITY_EVENTS_TABLE = "threads_activity_events"
OWN_POSTS_TABLE = "threads_posts"
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        self.client = client or httpx.Client(timeout=30)

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
