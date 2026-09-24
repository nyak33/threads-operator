# FUTURE / NOT CURRENTLY IMPLEMENTED

# Publish Recovery Observability — Future Design Note

Source: preserved from `origin/fix/global-publish-recovery` (deleted 2026-09-24). This document
captures design intent only. **No code, schema, or migration from that branch is active in
`main`.** The current `main` publish worker (`src/threads_operator/publish_worker.py`) already
provides durable retry with backoff, rate-limit detection, transient-vs-permanent classification,
and a per-attempt in-memory deadline. It does **not** persist the schema described below.

Do not copy `migrations/007_publish_recovery.sql` (or any portion of it) into the active
migration chain unless every "Preconditions" item in §4 is satisfied.

---

## 1. `retry_deadline_at timestamptz`

**Purpose.** A persistent, per-row cutoff after which automatic recovery must stop and the row
must transition to a terminal non-publishing state.

**Why it could be useful.** Today's worker computes an attempt deadline in memory
(`attempt_deadline_seconds`, default 30 minutes) at the moment a row is claimed. That deadline:

- lives only for the duration of the worker process,
- cannot be inspected by an operator (`select retry_deadline_at …` returns nothing),
- resets implicitly if the process restarts and re-claims the same row,
- cannot be audited historically ("when did this row stop being retry-eligible?").

Persisting `retry_deadline_at` makes the cutoff:

- **Observable** — queryable from Supabase without log access.
- **Durable across restarts** — a crashed worker does not silently extend the window by
  re-claiming with a fresh in-memory deadline.
- **Auditable** — the moment a row gave up is part of the row, not a log line.

**Difference from `attempt_deadline_seconds`.** The existing parameter is a *behaviour tuning
knob* (how long one claim will keep retrying). `retry_deadline_at` would be a *state field* (the
absolute timestamp after which this row must not be published). They are complementary: the
former derives the latter once at first failure, the latter is then consulted on every re-claim.

---

## 2. `last_error_meta jsonb`

**Purpose.** A credential-safe structured bag of Meta publish diagnostics for the latest failure.

**Intended safe fields** (and only these — never tokens, never account secrets):

- `meta_code` — Meta's top-level error code (e.g. `10`, `190`, `368`).
- `meta_subcode` — Meta's subcode when present (e.g. `33` on `GET /{web_id}`).
- `meta_type` / `category` — Meta's error type string (e.g. `OAuthException`) or an operator-side
  coarse category (`transport`, `rate_limit`, `permanent`, `unknown`).
- `http_status` — the HTTP status returned by the Graph call.
- `attempt_index` — which retry attempt produced this entry.
- `ts` — UTC timestamp of the failure.

**Why this improves on `last_error string`.** The current `last_error` is a free-form text
summary. It is fine for human eyeballs but poor for:

- grouping failures by root cause across rows (`where last_error_meta->>'meta_code' = '190'`),
- distinguishing rate-limit from auth-failure at the SQL layer (today that needs log access),
- alerting on specific Meta codes (e.g. page-token expiry) without parsing strings,
- driving operator dashboards that count failures by category over time.

A `jsonb` column is preferred over discrete columns because Meta's diagnostic shape evolves and
because the "safe" subset is small and stable — the schema does not need to chase every field
Meta adds.

---

## 3. `retrying` / `needs_attention` state concept

**Purpose.** Split the current binary `failed`/`approved` lifecycle into a more honest three-state
recovery model:

- `retrying` — a row that has failed at least once, is within its recovery window, and is
  eligible for automatic re-claim at `next_retry_at`.
- `needs_attention` — a row that has either (a) hit a clearly permanent error (OAuth, revoked
  permission, account restriction), or (b) exhausted its `retry_deadline_at`. Not eligible for
  automatic publish. Requires human/operator review.

**Why this matters.** Today a row that exhausts retries looks the same as a row that failed on a
permanent error. Operators cannot tell from the queue alone whether to investigate, wait, or
discard. Separating the states lets the operator:

- alert only on `needs_attention` (high signal),
- ignore `retrying` (background noise),
- report on recovery effectiveness (`retrying → posted` rate).

---

## 4. Preconditions for implementing this design

Do **not** implement any of the above until **all** of the following are true:

1. **Actual operational need.** A concrete incident or recurring operator pain point where the
   absence of `retry_deadline_at` / `last_error_meta` / `needs_attention` caused missed
   diagnosis, missed alert, or incorrect retry behaviour. "It would be nice" is not a need.
2. **Code consumers exist.** At least one piece of live code (worker, CLI, dashboard, alerting
   hook) reads or writes each new field/state. No dormant schema.
3. **Tests.** Unit + integration coverage for every new column and state transition, including
   the boundary at `retry_deadline_at` (must not publish after) and the safe-field whitelist on
   `last_error_meta` (must never serialise tokens).
4. **Migration.** A new forward-only migration (not a copy of `007_publish_recovery.sql`)
   reviewed against the *then-current* `threads_publish_queue` schema on production.
5. **Production verification.** After migration + deploy, run a synthetic failure on a test
   account and verify the new fields populate and the terminal transition fires at the deadline.

If any precondition is missing, this design stays in `docs/future/` and no schema change is made.
