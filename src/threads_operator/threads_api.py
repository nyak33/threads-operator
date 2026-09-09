"""Read-only Threads Graph API adapter for owned posts and Insights."""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_BASE_URL = "https://graph.threads.net/v1.0"
POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
ACCOUNT_METRICS = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "clicks",
    "followers_count",
)


class ThreadsAPI:
    def __init__(
        self,
        access_token: str,
        user_id: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("Threads access token is required")
        if not user_id:
            raise ValueError("Threads user id is required")
        self.access_token = access_token
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=30)

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"access_token": self.access_token}
        if extra:
            params.update(extra)
        return params

    def list_posts(self, limit: int = 100) -> list[dict[str, Any]]:
        response = self.client.get(
            f"{self.base_url}/{self.user_id}/threads",
            params=self._params(
                {
                    "fields": "id,text,timestamp,media_type,permalink",
                    "limit": limit,
                }
            ),
        )
        response.raise_for_status()
        posts: list[dict[str, Any]] = []
        for item in response.json().get("data", []):
            if not item.get("id"):
                continue
            posts.append(
                {
                    "id": item["id"],
                    "text": item.get("text"),
                    "timestamp": item.get("timestamp"),
                    "media_type": item.get("media_type"),
                    "permalink": item.get("permalink"),
                }
            )
        return posts

    def get_post_insights(self, post_id: str) -> dict[str, int | float | None]:
        return self._get_insights(
            f"{self.base_url}/{post_id}/insights",
            POST_METRICS,
        )

    def get_account_insights(self) -> dict[str, int | float | None]:
        return self._get_insights(
            f"{self.base_url}/{self.user_id}/threads_insights",
            ACCOUNT_METRICS,
        )

    def _get_insights(
        self,
        url: str,
        metric_names: tuple[str, ...],
    ) -> dict[str, int | float | None]:
        result: dict[str, int | float | None] = {name: None for name in metric_names}
        response = self.client.get(
            url,
            params=self._params({"metric": ",".join(metric_names)}),
        )
        if response.status_code != 400:
            response.raise_for_status()
            _merge_metric_payload(result, response.json())
            return result

        # Some post types/accounts may reject a combined request when one metric
        # is unavailable. Retry individually so unsupported metrics stay unknown
        # instead of losing the entire snapshot. Authentication/rate failures
        # still surface immediately.
        for metric in metric_names:
            single = self.client.get(
                url,
                params=self._params({"metric": metric}),
            )
            if single.status_code == 400:
                continue
            single.raise_for_status()
            _merge_metric_payload(result, single.json())
        return result


def _merge_metric_payload(
    result: dict[str, int | float | None], payload: dict[str, Any]
) -> None:
    for item in payload.get("data", []):
        name = item.get("name")
        if name in result:
            result[name] = _extract_scalar_value(item)


def _extract_scalar_value(item: dict[str, Any]) -> int | float | None:
    values = item.get("values")
    candidate: Any = None
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            candidate = first.get("value")
    if candidate is None:
        total_value = item.get("total_value")
        if isinstance(total_value, dict):
            candidate = total_value.get("value")
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, (int, float)):
        return candidate
    return None
