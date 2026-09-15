"""Parse Threads Activity → Follows payloads into normalized follow events.

Pure functions only: no browser, no network, no secrets. The collector feeds
this the server-prefetched ``BarcelonaActivityFeedStoryListContainerQuery``
JSON found in the /activity/follows document.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_TITLE_USER = re.compile(r"\{([^{}|]+)\|")
_GROUP_N = re.compile(r"(?:and\s+)?(\d+)\s+others?", re.IGNORECASE)
_REQUEST_CONTEXT = re.compile(r"\brequested\b", re.IGNORECASE)
FOLLOW_ICON = "follow"


def _parse_json_number(raw: str | bytes) -> Any:
    import json
    return json.loads(raw)


def extract_notification_nodes(payload: Any) -> list[dict]:
    """Walk an Activity query payload and return all feed story nodes."""
    found: list[dict] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            notifications = obj.get("notifications")
            if isinstance(notifications, dict) and isinstance(notifications.get("edges"), list):
                for edge in notifications["edges"]:
                    if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                        found.append(edge["node"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return found


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _follower_usernames(title: str) -> list[str]:
    return _TITLE_USER.findall(title or "")


def parse_notification_node(node: dict) -> dict[str, Any] | None:
    """Convert one feed story node to an attribution event, or None."""
    args = node.get("args")
    if not isinstance(args, dict):
        return None
    extra = args.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    context = (extra.get("context") or "").strip()
    title = extra.get("title") or ""
    icon = (extra.get("icon_name") or "").strip().lower()

    users = _follower_usernames(title)
    grouped_others = max(len(users) - 1, 0)
    m = _GROUP_N.search(title + " " + context)
    if m:
        grouped_others = max(grouped_others, int(m.group(1)))

    if icon and icon != FOLLOW_ICON:
        return None
    if _REQUEST_CONTEXT.search(context) or _REQUEST_CONTEXT.search(title):
        return None
    if "follow" not in context.lower() and icon != FOLLOW_ICON:
        return None

    notification_id = str(args.get("tuuid") or "").strip()
    snippet_raw = extra.get("content") or ""
    attribution = "from your post" in context.lower()
    if not attribution or not str(snippet_raw).strip():
        return None
    return {
        "notification_id": notification_id,
        "notification_datetime": _iso(args.get("timestamp")),
        "notification_type": icon or "follow",
        "visible_username": users[0] if users else "",
        "source_snippet_raw": snippet_raw,
        "context_raw": context,
        "grouped_others_count": grouped_others,
        "follow_count": 1 + grouped_others,
        "attribution_possible": attribution,
        "story_type": node.get("story_type"),
    }


def parse_activity_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse a full Activity payload into post-attribution follow events."""
    events = []
    seen_ids: set[str] = set()
    for node in extract_notification_nodes(payload):
        event = parse_notification_node(node)
        if event is None:
            continue
        key = event["notification_id"] or f"anon-{len(seen_ids)}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        events.append(event)
    return events
