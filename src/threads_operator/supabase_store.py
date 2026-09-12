"""Minimal Supabase REST adapter for append-only Threads Insights snapshots."""

from __future__ import annotations

from typing import Any

import httpx

from .threads_api import POST_METRICS, has_usable_metrics

ACCOUNT_TABLE = "threads_account_insights_snapshots"
POST_TABLE = "threads_post_insights_snapshots"


class SupabaseStore:
    def __init__(
        self,
        base_url: str,
        service_role_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Supabase URL is required")
        if not service_role_key:
            raise ValueError("Supabase service role key is required")
        self.base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
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
        self,
        captured_since: str,
        *,
        page_size: int = 1000,
    ) -> dict[str, dict[str, Any]]:
        """Return the newest usable recent snapshot for each post in batched reads."""
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
        if not rows:
            return None
        return rows[0]

    def _insert(self, table: str, payload: dict[str, Any]) -> None:
        headers = {**self._headers, "Prefer": "return=minimal"}
        response = self.client.post(
            f"{self.base_url}/rest/v1/{table}",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
