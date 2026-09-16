"""Threads Graph API adapter for owned posts, Insights, and text publishing."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

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


def has_usable_metrics(row: dict[str, Any]) -> bool:
    """Return True when at least one core post metric is present, including zero."""
    return any(row.get(name) is not None for name in POST_METRICS)


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Threads API base URL must use the official Threads API") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.threads.net"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("Threads API base URL must use the official Threads API")
    return base_url.rstrip("/")


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
        self.base_url = _validate_base_url(base_url)
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

    def get_post_identity(self, post_id: str) -> dict[str, Any]:
        """Fetch {id, permalink, username} for ONE post by its numeric id.

        Official Graph API only (base URL is host-validated to
        graph.threads.net). Used by Activity-candidate ingress to bridge a
        numeric fbid permalink to its canonical shortcode permalink. Raises
        ValueError unless the response echoes the exact requested id — a
        mismatched echo means the identity is NOT proven (fail closed).
        """
        post_id = str(post_id or "").strip()
        if not post_id.isdigit():
            raise ValueError("post_id must be a numeric Threads media id")
        response = self.client.get(
            f"{self.base_url}/{post_id}",
            params=self._params({"fields": "id,permalink,username"}),
        )
        response.raise_for_status()
        data = response.json() or {}
        if str(data.get("id") or "") != post_id:
            raise ValueError(
                "Threads API identity echo mismatch — refusing unproven id")
        if not data.get("permalink"):
            raise ValueError("Threads API returned no permalink for this id")
        return data

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

    def create_text_container(
        self, text: str, reply_to_id: str | None = None
    ) -> str:
        """Create a Threads TEXT media container and return its creation id."""
        if not text or not text.strip():
            raise ValueError("Threads text must not be empty")
        data: dict[str, str] = {
            "access_token": self.access_token,
            "media_type": "TEXT",
            "text": text,
        }
        if reply_to_id:
            data["reply_to_id"] = str(reply_to_id)
        response = self.client.post(
            f"{self.base_url}/{self.user_id}/threads",
            data=data,
        )
        response.raise_for_status()
        creation_id = response.json().get("id")
        if not creation_id:
            raise ValueError("Threads create response did not include id")
        return str(creation_id)

    def publish_container(self, creation_id: str) -> str:
        """Publish one existing media container and return the Threads post id."""
        if not creation_id:
            raise ValueError("Threads creation id is required")
        response = self.client.post(
            f"{self.base_url}/{self.user_id}/threads_publish",
            data={
                "access_token": self.access_token,
                "creation_id": str(creation_id),
            },
        )
        response.raise_for_status()
        post_id = response.json().get("id")
        if not post_id:
            raise ValueError("Threads publish response did not include id")
        return str(post_id)

    def publish_text(
        self,
        text: str,
        reply_to_id: str | None = None,
        *,
        max_attempts: int = 4,
        retry_delay_seconds: float = 2.0,
    ) -> str:
        """Create and publish text, retrying only transient publish readiness failures."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        creation_id = self.create_text_container(text, reply_to_id=reply_to_id)
        for attempt in range(max_attempts):
            try:
                return self.publish_container(creation_id)
            except httpx.HTTPStatusError as exc:
                if attempt >= max_attempts - 1 or not _is_transient_publish_error(
                    exc.response
                ):
                    raise
                if retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds * (2**attempt))
        raise RuntimeError("unreachable publish retry state")

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

        if not _is_metric_availability_error(response):
            response.raise_for_status()

        # Some post types/accounts may reject a combined request when one metric
        # is unavailable. Retry individually so unsupported metrics stay unknown
        # instead of losing the entire snapshot. Authentication and other
        # non-metric request failures are surfaced immediately.
        for metric in metric_names:
            single = self.client.get(
                url,
                params=self._params({"metric": metric}),
            )
            if single.status_code == 400 and _is_metric_availability_error(single):
                continue
            single.raise_for_status()
            _merge_metric_payload(result, single.json())
        return result


def _is_transient_publish_error(response: httpx.Response) -> bool:
    if response.status_code == 429 or response.status_code >= 500:
        return True
    if response.status_code != 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    if "oauth" in str(error.get("type", "")).lower():
        return False
    message = str(error.get("message", "")).lower()
    return any(
        marker in message
        for marker in (
            "still processing",
            "not ready",
            "media processing",
            "processing media",
        )
    )


def _is_metric_availability_error(response: httpx.Response) -> bool:
    """Return True only for 400 responses that specifically concern metrics."""
    if response.status_code != 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    error_type = str(error.get("type", "")).lower()
    if "oauth" in error_type:
        return False
    message = str(error.get("message", "")).lower()
    return "metric" in message


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
        dict_values = [value for value in values if isinstance(value, dict)]
        if dict_values:
            dated_values = [value for value in dict_values if value.get("end_time")]
            if dated_values:
                selected = max(dated_values, key=lambda value: str(value["end_time"]))
            else:
                selected = dict_values[-1]
            candidate = selected.get("value")
    if candidate is None:
        total_value = item.get("total_value")
        if isinstance(total_value, dict):
            candidate = total_value.get("value")
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, (int, float)):
        return candidate
    return None
