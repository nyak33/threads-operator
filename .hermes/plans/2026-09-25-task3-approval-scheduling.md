# Task #3 — Trend Engagement: Approval → Smart Scheduling

## Current (broken) flow — verified in code

`TrendEngagementDispatcher` callback `trendeng:approve:<id>` → CLI `_run_trendeng_approve` (operator_cli.py:1116):
1. guard `used_in_queue_id` / `status != pending_approval`
2. CAS `pending_approval → approved`
3. `store.enqueue_draft(...)` → INSERT queue row with **`status="draft"`** (hardcoded in supabase_store.py:1370)
4. `update_trend_candidate_workflow_a(status="queued", used_in_queue_id=qid)`

`publish_worker` only publishes rows with `status="approved"` (`peek_due_post` filter `status=eq.approved`).
**Nothing ever transitions the queue row `draft → approved`.** → approved content is stuck as a draft forever.
Live evidence: candidate #146 → queue #597, `status=draft`.

`_run_trendeng_use` (backlog) has the IDENTICAL defect (same `enqueue_draft` + `queued`, no promotion).

## Root cause
The approval path conflates "content approved" with "create queue row" and delegates the queue row
to `enqueue_draft`, which is designed for *unscheduled drafts* (status=draft). There is no scheduling
step and no `draft→approved` promotion for this ingress.

## New flow (this task)
Approve (content) → scheduling card → pick time → queue row `approved` + `scheduled_at` → publisher posts.
Candidate ends `queued` with `used_in_queue_id`.

New callbacks (prefix `trendsched:`):
- `trendsched:best:<cid>`    → compute best time (insights → fallback), set approved+scheduled
- `trendsched:now:<cid>`     → set approved, scheduled_at=now
- `trendsched:choose:<cid>`  → start custom-time session (reply with time)
- `trendsched:confirm:<cid>` → confirm parsed custom time → approved+scheduled
- `trendsched:cancel:<cid>`  → cancel scheduling (content NOT published; stays approved, resumable)

## Design decisions
- Queue row created as `approved` directly via a new store method `enqueue_approved(...)` (NOT
  enqueue_draft, which is intentionally draft-only). scheduled_at set at insert → atomic, no draft window.
- Idempotency: candidate `used_in_queue_id` reused/promoted if a draft row already exists for it
  (e.g. legacy #597) instead of inserting a duplicate. Double-taps safe (CAS + used_in_queue_id guard).
- Smart scheduling: deterministic slot scoring from `threads_post_insights_snapshots` +
  `threads_post_metric_snapshots` (both currently empty) + posted queue history (scheduled_at hour).
  Normalized engagement-rate scoring, min-sample gate, day-of-week + hour buckets, collision spacing,
  graceful fallback to configured windows when data insufficient. Single source in scheduling.py.
- Timezone: user-facing MYT (Asia/Kuala_Lumpur); stored as UTC timestamptz (existing convention).
- Session state: reuse `PendingEditStore` file-backed session pattern (30-min TTL) for custom-time input.
- Cancel: content stays `approved` (not published), candidate stays `approved`, resumable via re-approve.
- `_run_trendeng_use` routed through the same scheduling flow (keeps §15 'Use' working, no stuck draft).
- Fix latent bug: TRENDENG_ACTIONS missing "use" (backlog use would fail parse) — add it.
