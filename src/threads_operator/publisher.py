"""Deterministic publisher for one account-scoped Supabase queue.

Publishing is resumable. The row's external state (root id + ordered reply
ids) is persisted after EVERY successful publish step, and any resume of a
partially published thread first RECONCILES against live Threads state via the
official Graph API before publishing anything:

* the root is never recreated while ``threads_main_post_id`` is set, and a
  resume with no saved root id first scans the account feed for a live post
  with the exact root text (covers Meta-success / DB-persistence-failure);
* a reply that Meta accepted but the DB never recorded is ADOPTED by exact
  text match instead of being republished — confirmed or live replies are
  never intentionally duplicated;
* a persisted parent Meta refuses as a reply target (code 24 / subcode
  4279009 — the ghost-branch failure mode seen in production) is healed by
  hopping to a live sibling carrying the same reply text;
* if reconciliation cannot answer (API errors mid-scan) the row is isolated
  with a duplicate-risk ambiguity error — one row never blocks the queue and
  no blind republish ever happens.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .supabase_store import SupabaseStore
from .threads_api import ThreadsAPI
from .topic import resolve_topic

# Feed-scan budget for root-duplicate detection (posts, not pages).
ROOT_SCAN_LIMIT = 300


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


def _is_ghost_parent_rejection(exc: Exception) -> bool:
    """True for Meta's 'requested resource does not exist' on reply_to_id
    (code 24 / subcode 4279009) — the parent post cannot be replied to
    (deleted or ghost branch), while the reply content itself is fine."""
    text = str(exc)
    return "subcode=4279009" in text or '"error_subcode":4279009' in text


class _Reconciler:
    """Live-thread reconciliation for a partially published thread.

    Walks the reply chain with ``GET /{post-id}/replies`` matching texts
    against the expected reply_texts, so the checkpoint is corrected (adopt
    published-but-unpersisted replies, drop dead ids) BEFORE any new publish.
    Every read is cached per node per resume; a failed read marks the whole
    reconciliation non-authoritative so callers fail closed instead of guessing.
    """

    def __init__(self, api: ThreadsAPI, replies: list[str]):
        self.api = api
        self.replies = replies
        self._children: dict[str, list[dict[str, Any]] | None] = {}
        self._feed: list[dict[str, Any]] | None = None
        self._feed_complete = False
        self._failed = False

    @property
    def supported(self) -> bool:
        return hasattr(self.api, "list_direct_replies")

    @property
    def failed(self) -> bool:
        """True when any reconciliation read errored (not authoritative)."""
        return self._failed

    def children(self, node: str) -> list[dict[str, Any]] | None:
        """Direct replies of ``node``, or None when unknown (read failed)."""
        if node not in self._children:
            try:
                self._children[node] = self.api.list_direct_replies(node)  # type: ignore[attr-defined]
            except (httpx.HTTPError, ValueError):
                self._children[node] = None
                self._failed = True
        return self._children[node]

    def find_child_with_text(self, parent: str, text: str) -> str | None:
        """Id of a live direct reply under ``parent`` whose text matches
        exactly, or None when there is none / the read failed (see .failed)."""
        kids = self.children(parent)
        if kids is None:
            return None
        want = text.strip()
        matches = [k["id"] for k in kids if (k.get("text") or "").strip() == want]
        if not matches:
            return None
        # If several same-text duplicates exist under one parent (an earlier
        # crash published twice), the first is adopted — adopting never
        # creates a new post, whichever id we record.
        return matches[0]

    def scan_account_feed(self) -> list[dict[str, Any]] | None:
        """Recent own posts [{id, text, timestamp}] or None if the read failed.
        ``feed_complete`` tells whether the scan proved full coverage."""
        if self._feed is None:
            try:
                self._feed = self.api.list_posts(limit=100) or []
                self._feed_complete = True
            except (httpx.HTTPError, ValueError):
                self._feed = None
                self._feed_complete = False
                self._failed = True
        return self._feed

    def find_root_by_text(
        self, text: str, *, not_before: Any = None
    ) -> tuple[str | None, bool]:
        """Return (post_id, conclusive). conclusive=False when the feed scan
        could not prove there is no matching root (read failed). Only posts
        at/after ``not_before`` count, so an older same-text post from another
        campaign can never be falsely adopted as this row's root."""
        feed = self.scan_account_feed()
        if feed is None:
            return None, False
        want = text.strip()
        for post in feed:
            if (post.get("text") or "").strip() == want and post.get("id"):
                if _posted_before(post.get("timestamp"), not_before):
                    continue
                return str(post["id"]), True
        return None, self._feed_complete


def _posted_before(timestamp: Any, not_before: Any) -> bool:
    """True when the post timestamp is provably older than the queue row's
    creation. Unparseable/missing values are treated as NOT older (conservative
    on adoption: matches are allowed, never silently skipped)."""
    from datetime import datetime, timezone

    if not timestamp or not not_before:
        return False
    try:
        ts = datetime.fromisoformat(str(timestamp).replace("+0000", "+00:00"))
        lower = datetime.fromisoformat(str(not_before).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if lower.tzinfo is None:
        lower = lower.replace(tzinfo=timezone.utc)
    return ts < lower


def publish_next(
    api: ThreadsAPI,
    store: SupabaseStore,
    table: str,
    campaign_code: str | None = None,
    *,
    dry_run: bool = False,
    claimed_row: dict[str, Any] | None = None,
    max_reply_hops: int = 2,
) -> dict[str, Any]:
    """Publish the oldest due approved queue row after winning its claim.

    Dry-run only peeks. Live execution always claims first (or resumes an
    already-claimed row passed via ``claimed_row`` — the stale-posting reclaim
    path). Known external IDs are persisted after each publish step, and a
    row carrying partial progress is reconciled against live state before
    anything new is published, so resuming never duplicates.
    """
    if dry_run:
        candidate = store.peek_due_post(table, campaign_code=campaign_code)
        if not candidate:
            return {"status": "idle"}
        return {"status": "dry-run", "queue_id": candidate.get("id")}

    if claimed_row is not None:
        row = claimed_row
    else:
        row = store.claim_due_post(table, campaign_code=campaign_code)
        if not row:
            return {"status": "idle"}

    row_id = row.get("id")
    if row_id is None:
        raise ValueError("Claimed queue row is missing id")

    main_post_id: str | None = None
    reply_ids: list[str] = []
    replies: list[str] = []
    step = "main"
    try:
        main_text = row.get("main_post_text")
        if not isinstance(main_text, str) or not main_text.strip():
            raise ValueError("Claimed queue row has no main_post_text")
        replies = _reply_texts(row)

        # The topic lives on the row, so it survives every claim/requeue/
        # recovery cycle untouched. It is attached to the root post only —
        # Meta allows one topic per post, and replies inherit the thread's
        # context. Missing/blank topics resolve to None and publish untagged.
        topic_tag = resolve_topic(table, row)

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

        resumed = main_post_id is not None or bool(reply_ids)
        prior_attempts = 0
        try:
            prior_attempts = int(row.get("attempt_count") or 0)
        except (TypeError, ValueError):
            prior_attempts = 0
        if prior_attempts > 0:
            resumed = True
        reconciler = _Reconciler(api, replies)

        if main_post_id is None and not reply_ids and reconciler.supported and prior_attempts > 0:
            # Crash between root publish and persistence leaves NO ids behind
            # (row was requeued by the worker). Before creating a new root,
            # prove none exists live for this exact text.
            found, conclusive = reconciler.find_root_by_text(
                main_text, not_before=row.get("created_at")
            )
            if found:
                main_post_id = found
                store.mark_post_main_published(table, row_id, found)
            elif not conclusive:
                raise ValueError(
                    "Duplicate-risk ambiguity: root-duplicate feed scan "
                    "unavailable before republishing an attempted row"
                )

        if main_post_id is None:
            step = "main"
            main_post_id = api.publish_text(main_text, topic_tag=topic_tag)
            store.mark_post_main_published(table, row_id, main_post_id)

        # Reconcile the persisted chain BEFORE publishing more replies.
        if resumed and reply_ids and reconciler.supported:
            reply_ids = _verified_prefix(reconciler, str(main_post_id), reply_ids)
            store.mark_post_reply_progress(table, row_id, reply_ids)

        parent_id = reply_ids[-1] if reply_ids else main_post_id
        chain: list[Any] = [main_post_id, *reply_ids]
        index = len(reply_ids)
        while index < len(replies):
            reply_text = replies[index]
            step = f"reply {index + 1}/{len(replies)} (parent {parent_id})"

            # Idempotence gate: if a live reply with this exact text already
            # hangs under the current parent (Meta accepted, DB never recorded),
            # ADOPT it instead of publishing a duplicate.
            if reconciler.supported:
                existing = reconciler.find_child_with_text(str(parent_id), reply_text)
                if existing:
                    reply_ids.append(existing)
                    chain.append(existing)
                    index += 1
                    store.mark_post_reply_progress(table, row_id, reply_ids)
                    parent_id = existing
                    continue
                if reconciler.failed and index > 0:
                    raise ValueError(
                        "Duplicate-risk ambiguity: live reconciliation "
                        "unavailable for a partially published thread"
                    )

            try:
                reply_id = api.publish_text(reply_text, reply_to_id=parent_id)
            except httpx.HTTPStatusError as exc:
                if not (_is_ghost_parent_rejection(exc) and reconciler.supported):
                    raise
                healed = _hop_to_live_sibling(
                    reconciler, chain, replies, parent_id, max_hops=max_reply_hops
                )
                if healed is None:
                    raise
                parent_id = healed
                step = (
                    f"reply {index + 1}/{len(replies)} "
                    f"(recovered parent {parent_id})"
                )
                chain[-1] = parent_id
                reply_ids[index - 1] = parent_id
                store.mark_post_reply_progress(table, row_id, reply_ids)
                continue  # re-enter: idempotence gate re-checks the new parent
            reply_ids.append(reply_id)
            chain.append(reply_id)
            index += 1
            store.mark_post_reply_progress(table, row_id, reply_ids)
            parent_id = reply_id

        store.mark_post_posted(table, row_id, reply_ids)
        return {
            "status": "posted",
            "queue_id": row_id,
            "main_post_id": main_post_id,
            "reply_ids": reply_ids,
            "resumed": resumed,
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if step != "main":
            error = f"{error} [step: {step}]"
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
        result["expected_replies"] = len(replies)
        result["completed_replies"] = len(reply_ids)
        return result


def _verified_prefix(
    reconciler: _Reconciler, main_post_id: str, reply_ids: list[str]
) -> list[str]:
    """Longest prefix of the persisted checkpoint that is live-verified as a
    child chain of the root. Unknown reads trust the remainder (never discard
    ids we cannot disprove); a dead id truncates the checkpoint so the resume
    republishes that slot under the last verified parent."""
    verified: list[str] = []
    current = main_post_id
    for rid in reply_ids:
        kids = reconciler.children(current)
        if kids is None:
            return verified + reply_ids[len(verified):]
        if rid in [k["id"] for k in kids]:
            verified.append(rid)
            current = rid
        else:
            break
    return verified


def _hop_to_live_sibling(
    reconciler: _Reconciler,
    chain: list[Any],
    replies: list[str],
    parent_id: str,
    *,
    max_hops: int = 2,
) -> str | None:
    """After a ghost-parent rejection, find the live sibling that really hosts
    this branch: a different child of the grandparent carrying the parent's own
    reply text. The caller retries against it (the idempotence gate re-checks
    its children first)."""
    if max_hops < 1 or len(chain) < 2 or chain[-2] is None:
        return None
    grandparent = str(chain[-2])
    parent_text_index = len(chain) - 2  # replies[idx] is chain[-1]'s own text
    if parent_text_index < 0 or parent_text_index >= len(replies):
        return None
    want = replies[parent_text_index].strip()
    kids = reconciler.children(grandparent)
    if kids is None:
        return None
    for k in kids:
        if k["id"] != str(parent_id) and (k.get("text") or "").strip() == want:
            return k["id"]
    return None
