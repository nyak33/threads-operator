"""Deterministic publisher for one account-scoped Supabase queue."""
from __future__ import annotations

import json
from typing import Any

from .supabase_store import SupabaseStore
from .threads_api import ThreadsAPI


def _reply_texts(row: dict[str, Any]) -> list[str]:
    raw = row.get("reply_texts")
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("reply_texts must be a JSON array") from exc
    if not isinstance(raw, list):
        raise ValueError("reply_texts must be an array")
    replies: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("reply_texts entries must be strings")
        text = item.strip()
        if text:
            replies.append(item)
    return replies


def publish_next(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Publish the oldest due approved queue row after winning its claim.

    Dry-run only peeks. Live execution always claims first. Known external IDs
    are persisted before the next publish step so partial failures remain visible
    for manual reconciliation rather than being blindly retried.
    """
    if dry_run:
        candidate = store.peek_due_post(table, campaign_code=campaign_code)
        if not candidate:
            return {"status": "idle"}
        return {"status": "dry-run", "queue_id": candidate.get("id")}

    row = store.claim_due_post(table, campaign_code=campaign_code)
    if not row:
        return {"status": "idle"}

    row_id = row.get("id")
    if row_id is None:
        raise ValueError("Claimed queue row is missing id")

    main_post_id: str | None = None
    reply_ids: list[str] = []
    try:
        main_text = row.get("main_post_text")
        if not isinstance(main_text, str) or not main_text.strip():
            raise ValueError("Claimed queue row has no main_post_text")
        replies = _reply_texts(row)

        existing_main = row.get("threads_main_post_id")
        if existing_main is not None and str(existing_main).strip():
            main_post_id = str(existing_main)
        raw_reply_ids = row.get("threads_reply_ids") or []
        if isinstance(raw_reply_ids, str):
            try:
                raw_reply_ids = json.loads(raw_reply_ids)
            except json.JSONDecodeError as exc:
                raise ValueError("threads_reply_ids must be a JSON array") from exc
        if not isinstance(raw_reply_ids, list) or any(
            not isinstance(reply_id, str) or not reply_id.strip()
            for reply_id in raw_reply_ids
        ):
            raise ValueError("threads_reply_ids must contain non-empty strings")
        reply_ids = list(raw_reply_ids)

        if reply_ids and main_post_id is None:
            raise ValueError("Persisted reply IDs exist without threads_main_post_id")
        if len(reply_ids) > len(replies):
            raise ValueError("Persisted reply progress exceeds configured reply_texts")

        if main_post_id is None:
            main_post_id = api.publish_text(main_text)
            store.mark_post_main_published(table, row_id, main_post_id)

        parent_id = reply_ids[-1] if reply_ids else main_post_id
        for reply_text in replies[len(reply_ids):]:
            reply_id = api.publish_text(reply_text, reply_to_id=parent_id)
            reply_ids.append(reply_id)
            store.mark_post_reply_progress(table, row_id, reply_ids)
            parent_id = reply_id

        store.mark_post_posted(table, row_id, reply_ids)
        return {
            "status": "posted",
            "queue_id": row_id,
            "main_post_id": main_post_id,
            "reply_ids": reply_ids,
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            store.mark_post_failed(table, row_id, error)
        except Exception as persistence_exc:
            error = (
                f"{error}; failed to persist failure state: "
                f"{type(persistence_exc).__name__}: {persistence_exc}"
            )
        result: dict[str, Any] = {
            "status": "failed",
            "queue_id": row_id,
            "error": error,
        }
        if main_post_id is not None:
            result["main_post_id"] = main_post_id
        if reply_ids:
            result["reply_ids"] = reply_ids
        return result
