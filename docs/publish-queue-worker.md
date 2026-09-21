# Publish-Queue Worker (cron-driven publishing)

Deterministic, no-LLM worker that publishes approved rows from the account's
publish queue on a fixed schedule. This is the production flow used on the
VPS; it needs no agent, no browser, and no manual prompting once installed.

## What it does

On each run, the `publish-worker` command:

1. Selects the **oldest due** row for the account where
   `status = approved`, `scheduled_at <= now()`, and
   `threads_main_post_id IS NULL` (one row per run, oldest first). An optional
   `--campaign-code` narrows selection to one campaign; omit it to publish any
   due approved row for the account.
2. **Claims** the row atomically (`approved → posting`) so concurrent workers
   never double-publish.
3. Publishes the main post (and any replies) via the Threads Graph API
   container flow, persisting `threads_main_post_id` immediately after the
   root and the ordered `threads_reply_ids` after **every** reply, each write
   doubling as a `heartbeat_at` stamp (see *Partial-thread recovery* below).
4. Marks the row `posted` with `posted_at`, or `failed` with `last_error`.
   Before each run it also reclaims any half-published `posting` row whose
   heartbeat went stale (>10 min) and resumes it safely.
5. **Auto-requeues transient failures** (network, timeout, 429, 5xx, and the
   known flaky non-OAuth Meta 400) back to `approved` so the next tick retries
   automatically. OAuth/permission/permanent errors stay `failed` for manual
   review and are never auto-retried.

The API adapter additionally retries a publish once with a **fresh container**
when Meta keeps rejecting a publish (see `threads_api.publish_text`).

Duplicate protection comes from the atomic claim plus the
`threads_main_post_id IS NULL` selection guard — a posted row is never
re-claimed, so a post is never published twice.

## Install (fresh VPS / fresh Hermes)

1. Clone and bootstrap the operator, add the account, and fill its env file
   (see `RUNBOOK.md` → *Fresh VPS Bring-Up* and `accounts/example.env`).
   Required for live posting:
   ```
   THREADS_POSTING_ENABLED=true
   THREADS_EXECUTION_MODE=auto_post
   THREADS_QUEUE_TABLE=threads_publish_queue
   # THREADS_QUEUE_CAMPAIGN_CODE=...   # optional default campaign filter
   ```
2. Apply the queue migrations: `migrations/004_threads_publish_queue.sql`,
   then `008_publish_queue_retry_state.sql` (retry/backoff state) and
   `009_publish_queue_heartbeat.sql` (heartbeat checkpointing). The code
   degrades gracefully on tables where 009 is not yet applied.
3. Verify the worker in dry-run (no posting):
   ```bash
   .venv/bin/threads-operator publish-worker --account syaqir --dry-run
   ```
4. Install the cron wrapper [`scripts/publish_queue_worker_hermes.py`](../scripts/publish_queue_worker_hermes.py)
   as a Hermes cron job, for example every 5 minutes:
   ```bash
   hermes cron add \
     --name "Publish Queue Worker (syaqir)" \
     --schedule "*/5 * * * *" \
     --script /home/admin/threads-operator/scripts/publish_queue_worker_hermes.py \
     --no-agent
   ```
   Edit `ACCOUNT` (and optionally `CAMPAIGN_CODE`) in the wrapper for your
   deployment. Run one cron job per account.
5. Verify the job is live, not just configured:
   ```bash
   hermes cron list        # enabled, schedule, next run
   hermes cron logs <id>   # last_status=ok after a tick
   ```

## Queue rows

Rows are inserted by the content pipeline (or `enqueue-draft`) with
`status = draft`. Promote to `approved` (via Supabase or your approval step)
with a future `scheduled_at`; the worker posts each when its time arrives.
Rows are never posted early — selection requires `scheduled_at <= now()`.

## Failure handling

- **Transient** (auto): requeued to `approved` with persisted retry state
  (`attempt_count`, `last_attempt_at`, `next_retry_at`) and a backoff ladder of
  5m → 15m → 30m → 1h → 2h; retried automatically once the gate opens. OAuth/
  permission/permanent errors mark the row `failed` for manual review. After
  `max_persisted_attempts` (default 5) the row fails permanently. A broken row
  never blocks the queue: the worker continues with the next eligible due rows
  (bounded, sequential, oldest-first, max 3 rows per run per account).
- **Non-recoverable**: stays `failed` with `last_error`; fix the cause and
  manually set the row back to `approved` to retry.

Both `publish` (single-shot, no requeue) and `publish-worker` (cron, with
requeue) share the same underlying `publish_next` flow and safety rules.

## Partial-thread recovery (heartbeat + auto-reclaim)

Publishing is **resumable**. Every durable checkpoint is one atomic PATCH that
also stamps `heartbeat_at` (migration `009_publish_queue_heartbeat.sql`):

- claim (`approved → posting`) stamps `claimed_at` + `heartbeat_at`
- root published → `threads_main_post_id` persisted immediately
- each reply published → full ordered `threads_reply_ids` persisted immediately
- completion → `status = posted`, retry state cleared

If the worker process dies mid-thread, the row stays `posting` with a growing
heartbeat gap. On its next run, **before** normal selection, the worker calls
`reclaim_stale_posting_rows`: any `posting` row whose heartbeat (falling back
to `claimed_at`, then the heartbeat column being absent) is older than
`stale_seconds` (default 600 = 10 min) is re-claimed with a compare-and-swap
PATCH — the write only lands while the row is still `posting` *and* still
stale, so a healthy in-flight worker's row can never be stolen and two
reclaimers cannot both win. The reclaimed row is resumed **in the same run**
via `publish_next(claimed_row=...)`.

Resuming never duplicates:

1. `threads_main_post_id` exists → the root is **never** recreated; the run
   continues from the first unrecorded reply slot.
2. Persisted reply ids are treated as a checkpoint and verified against the
   live thread with the official `GET /{post-id}/replies` pagination before
   publishing more. A published-but-unrecorded reply is **adopted** (id
   persisted) rather than re-published.
3. A row requeued to `approved` with zero ids after attempts scans the account
   feed for an existing root with identical text first, restricted to posts
   created at/after the row; a provably-older same-text post is never falsely
   adopted.
4. Meta ghost-parent rejections (code 24 / subcode 4279009 — the recorded
   parent is a deleted or ghost branch) heal by hopping to the live sibling
   that actually carries the branch, retrying under it without duplicating.
5. **Duplicate-risk ambiguity:** if a partially published thread cannot be
   reconciled (live reads failing) or root existence cannot be proven absent,
   the publisher fails **closed** before publishing anything: only that row is
   isolated, a deduplicated Telegram alert fires, and unrelated queue rows
   continue normally.

Alerts (Telegram, deduplicated per row+attempt-step) cover: stale `posting`
reclaim failures, duplicate-risk ambiguity, max attempts reached, permanent
Meta rejections, and stalled rows — each including queue id, campaign,
scheduled time (MYT), attempts, root id, completed/expected replies, last
error, and the action already taken automatically.

`heartbeat_at` writes degrade gracefully: tables where migration 009 has not
been applied fall back to `claimed_at`-based staleness automatically.

