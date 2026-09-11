"""Minimal Supabase REST adapter for append-only Threads Insights snapshots."""

from __future__ import annotations

from typing import Any

import httpx


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
        self._insert("threads_account_snapshots", payload)

    def insert_post_snapshot(self, payload: dict[str, Any]) -> None:
        self._insert("threads_post_snapshots", payload)

    def latest_post_snapshot(self, post_id: str) -> dict[str, Any] | None:
        return self._latest_snapshot(
            "threads_post_snapshots", "post_id", post_id
        )

    def latest_account_snapshot(self, account_id: str) -> dict[str, Any] | None:
        return self._latest_snapshot(
            "threads_account_snapshots", "account_id", account_id
        )

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
