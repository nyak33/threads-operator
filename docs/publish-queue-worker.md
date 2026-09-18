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
   container flow, persisting `threads_main_post_id` immediately.
4. Marks the row `posted` with `posted_at`, or `failed` with `last_error`.
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
2. Apply the queue migration `migrations/004_threads_publish_queue.sql`.
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

- **Transient** (auto): requeued to `approved`; retried on the next tick. With
  a 5-minute interval and in-run retries, a flaky Meta publish typically
  recovers within ~30 minutes.
- **Non-recoverable**: stays `failed` with `last_error`; fix the cause and
  manually set the row back to `approved` to retry.

Both `publish` (single-shot, no requeue) and `publish-worker` (cron, with
requeue) share the same underlying `publish_next` flow and safety rules.
