"""Minimal Supabase REST adapter for append-only Threads Insights snapshots."""

from __future__ import annotations

import pathlib
from typing import Any

import httpx

from .threads_api import POST_METRICS, has_usable_metrics

ACCOUNT_TABLE = "threads_account_insights_snapshots"
POST_TABLE = "threads_post_insights_snapshots"
ACTIVITY_EVENTS_TABLE = "threads_activity_events"
OWN_POSTS_TABLE = "threads_posts"


class SupabaseStore:
    def __init__(self, base_url: str, service_role_key: str, client: httpx.Client | None = None) -> None:
        if not base_url:
            raise ValueError("Supabase URL is required")
        if not service_role_key:
            raise ValueError("Supabase service role key is required")
        self.base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.client = client or httpx.Client(timeout=30)

    @property
    def _headers(self) -> dict[str, str]:
        return {"apikey": self.service_role_key, "Authorization": f"Bearer {self.service_role_key}", "Content-Type": "application/json"}

    def insert_account_snapshot(self, payload: dict[str, Any]) -> None:
        self._insert(ACCOUNT_TABLE, payload)

    def insert_post_snapshot(self, payload: dict[str, Any]) -> None:
        self._insert(POST_TABLE, payload)

    def latest_post_snapshot(self, post_id: str) -> dict[str, Any] | None:
        return self._latest_snapshot(POST_TABLE, "post_id", post_id)

    def latest_post_snapshots(self, captured_since: str, *, page_size: int = 1000) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        offset = 0
        select = "post_id,captured_at," + ",".join(POST_METRICS)
        while True:
            response = self.client.get(f"{self.base_url}/rest/v1/{POST_TABLE}", headers=self._headers,
                params={"captured_at": f"gte.{captured_since}", "order": "captured_at.desc", "limit": str(page_size), "offset": str(offset), "select": select})
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

    def _latest_snapshot(self, table: str, key: str, value: str) -> dict[str, Any] | None:
        response = self.client.get(f"{self.base_url}/rest/v1/{table}", headers=self._headers,
            params={key: f"eq.{value}", "order": "captured_at.desc", "limit": "1", "select": "*"})
        response.raise_for_status()
        rows = response.json()
        return None if not rows else rows[0]

    def _insert(self, table: str, payload: dict[str, Any]) -> None:
        headers = {**self._headers, "Prefer": "return=minimal"}
        response = self.client.post(f"{self.base_url}/rest/v1/{table}", headers=headers, json=payload)
        response.raise_for_status()

    def read_activity_follows_sync(self, profile_dir: pathlib.Path, settle_seconds: float = 3.0) -> dict:
        from .activity_browser import read_activity_follows_sync
        return read_activity_follows_sync(profile_dir, settle_seconds=settle_seconds)

    def insert_activity_events(self, events: list[dict[str, Any]]) -> int:
        """Append only unseen Activity observation states."""
        if not events:
            return 0
        fingerprints = [str(evt.get("fallback_fingerprint") or "") for evt in events]
        fingerprints = [fp for fp in fingerprints if fp]
        existing: set[str] = set()
        if fingerprints:
            response = self.client.get(f"{self.base_url}/rest/v1/{ACTIVITY_EVENTS_TABLE}", headers=self._headers,
                params={"fallback_fingerprint": f"in.({','.join(fingerprints)})", "select": "fallback_fingerprint"})
            response.raise_for_status()
            existing = {str(row.get("fallback_fingerprint")) for row in response.json()}

        new_payloads: list[dict[str, Any]] = []
        seen = set(existing)
        for evt in events:
            fingerprint = str(evt.get("fallback_fingerprint") or "")
            is_new = bool(fingerprint) and fingerprint not in seen
            evt["upserted"] = is_new
            if not is_new:
                continue
            seen.add(fingerprint)
            new_payloads.append({k: v for k, v in evt.items() if k != "upserted"})

        if not new_payloads:
            return 0
        headers = {**self._headers, "Prefer": "return=minimal"}
        response = self.client.post(f"{self.base_url}/rest/v1/{ACTIVITY_EVENTS_TABLE}", headers=headers, json=new_payloads)
        response.raise_for_status()
        return len(new_payloads)

    def list_posts(self) -> list[dict[str, Any]]:
        response = self.client.get(f"{self.base_url}/rest/v1/{OWN_POSTS_TABLE}", headers=self._headers,
            params={"select": "thread_id,text,published_at,permalink"})
        response.raise_for_status()
        return response.json() or []
