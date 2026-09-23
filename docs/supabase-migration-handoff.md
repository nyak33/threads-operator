# Supabase Migration Handoff — Verification Record

Date: 2026-09-23. Branch `feat/plug-and-play-hardening` @ `63e8b95` (unchanged).
Read-only audit. **No production Supabase changes were made.**

This is the handoff requested before any branch cleanup / merge-to-main. It documents the exact migration path a brand-new Supabase database must apply, per-migration purpose/dependencies/replay-safety, assumption checks, and post-migration verification.

## 1. Ordered apply list (brand-new database)

**Dependency-corrected order.** This is NOT pure filename order — see the reordering note below.

1. `013_missing_base_tables_reconcile.sql`  ← apply FIRST on a fresh DB
2. `001_threads_insights.sql`
3. `001b_threads_insights_snapshots.sql`
4. `002_post_daily_rollups.sql`
5. `003_activity_follow_events.sql`
6. `004_threads_publish_queue.sql`
7. `005_activity_multi_account_upgrade.sql`
8. `006_security_hardening.sql`
9. `007_threads_engagement_queue.sql`
10. `008_publish_queue_retry_state.sql`
11. `009_publish_queue_heartbeat.sql`
12. `010_two_engagement_workflows.sql`
13. `011_fix_trend_queue_reference.sql`
14. `012_trend_engagement_backlog.sql`
15. `014_fresh_schema_security_reconcile.sql`  ← apply LAST (RLS/privilege reconcile)

**Why 013 must lead:** `010`, `011`, `012` all `ALTER public.threads_trend_candidates`, and `007` adds a FK `references public.threads_trend_candidates(id)`. No migration in `001`–`009` creates `threads_trend_candidates` — that is exactly the gap 013 closes. On a fresh DB, if 013 is not applied first, 007/010/011/012 fail with "relation does not exist." On the **existing production** DB this reorder is irrelevant (013 is fully idempotent there).

**Soft dependency:** `001b` adds columns/rollups that build on 001's snapshot design; 005 assumes 003's table exists; 008/009 assume 004's `threads_publish_queue` exists. The list above already respects all of these.

## 2. Per-migration

| # | File | Purpose | Depends on (earlier migrations / objects) | Safe on a fresh DB? |
|---|------|---------|-------------------------------------------|---------------------|
| 1 | `013_missing_base_tables_reconcile.sql` | Recreates 6 production-only base tables (`threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`) that ad-hoc SQL created but no migration captured. Additive + idempotent; 006-pattern grants. | None. **Foundational** — must precede 007/010/011/012. | **YES** — pure CREATE-if-not-exists. |
| 2 | `001_threads_insights.sql` | Insights snapshot base (account/post snapshots scaffolding). Enables pgcrypto. | None (self-contained base). | **YES** |
| 3 | `001b_threads_insights_snapshots.sql` | Snapshot tables + `threads_daily_rollups` + RLS + grants. | 001 (same snapshot domain; account/post insights lineage). | **YES** |
| 4 | `002_post_daily_rollups.sql` | `threads_post_daily_rollups` table + indexes + RLS + policy. Rebuilt from post snapshots. | Conceptually reads `threads_post_insights_snapshots` (001b), but only at rebuild-time — CREATE itself is standalone. | **YES** |
| 5 | `003_activity_follow_events.sql` | `threads_activity_events` (append-only follow-attribution) + `threads_follows_from_post_summary` view + RLS/grants. | None. | **YES** |
| 6 | `004_threads_publish_queue.sql` | `threads_publish_queue` (account-scoped publish queue) + indexes + RLS/grants. | None. | **YES** |
| 7 | `005_activity_multi_account_upgrade.sql` | Multi-account upgrade of the Activity table: adds `account_key`, backfills legacy rows, drops/recreates constraints+indexes, recreates the summary view. | **003** (alters `threads_activity_events`). | **YES** (guarded), but **data-mutating** — see §3. |
| 8 | `006_security_hardening.sql` | Forward-only privilege hardening on insights tables; guarded revokes for legacy snapshot tables. | 001/001b tables. | **YES** (all guards via `to_regclass`). |
| 9 | `007_threads_engagement_queue.sql` | `threads_engagement_queue` + approval-gate CHECKs + dedupe index + RLS/grants. | **013** (FK → `threads_trend_candidates`). | **YES** (after 013). |
| 10 | `008_publish_queue_retry_state.sql` | Adds retry/backoff columns to `threads_publish_queue` + partial index. | **004** (alters `threads_publish_queue`). | **YES** |
| 11 | `009_publish_queue_heartbeat.sql` | Adds `heartbeat_at` to `threads_publish_queue` + watchdog index. | **004**. | **YES** |
| 12 | `010_two_engagement_workflows.sql` | Widens `threads_trend_candidates` status CHECK; creates `threads_own_reply_engagement`. | **013** (alters `threads_trend_candidates`). | **YES** (guarded DO blocks). |
| 13 | `011_fix_trend_queue_reference.sql` | Fixes `used_in_queue_id` type (uuid→bigint), re-points FK to `threads_publish_queue(id)`. | **013 + 004** (alters trend table; FK → publish queue). | **YES** (guarded), but **data-mutating** — see §3. |
| 14 | `012_trend_engagement_backlog.sql` | Adds `backlog`/`discarded` statuses + approval-timing columns + timeout/backlog indexes. | **013** (alters trend table); extends 010's CHECK. | **YES** (guarded DO blocks). |
| 15 | `014_fresh_schema_security_reconcile.sql` | Reconciles fresh-install RLS/privilege posture with production: enables RLS + revokes anon/authenticated + grants service_role + idempotent service_role policy on the 7 operator tables. | **013 + 010** (the tables it secures). | **YES** (guarded DO blocks). |

## 3. Assumption checks

**Does any migration assume existing production data?**
- **No migration requires production data to function.** All CREATE paths are data-independent.
- **Two migrations are data-mutating** and behave differently depending on whether rows exist. They are still safe on a fresh (empty) DB — the mutations are no-ops on zero rows:
  - `005` — `update threads_activity_events set account_key='legacy' where account_key is null;` then sets the column `not null`. On an existing DB with rows this is a **real data rewrite** (assigns the placeholder key `'legacy'` to all pre-multi-account rows). On a fresh DB it is a no-op.
  - `011` — `update threads_trend_candidates set used_in_queue_id=null where used_in_queue_id is not null;` before the type change. The migration header documents that only synthetic/test rows had non-null values. On a fresh DB it is a no-op.
- **Net:** fresh-DB replay needs no seed data; production replay of 005/011 touches real rows (by design, both already applied in prod).

**Does any migration assume manually created tables/functions?**
- **Tables:** YES — 007/010/011/012 assume `threads_trend_candidates`, which was created manually in production (never by a migration). **013 resolves this** by recreating it; that is why 013 must lead a fresh replay. No other manual-table dependency remains once 013 is in the chain.
- **Functions:** NO. No migration calls a user-defined function that it does not itself create. (`gen_random_uuid()` is pgcrypto-built-in; no custom `set_updated_at()`/trigger functions are used anywhere — `updated_at` is maintained by application code, not triggers.)

**Hardcoded generated IDs?**
- **NO.** No migration inserts fixed UUIDs or fixed identity values. UUID/identity defaults are generated at insert-time (`gen_random_uuid()`, `generated by default as identity`). The only hardcoded *value* is the placeholder `account_key='legacy'` in 005's backfill (a data label, not an ID reference).

**Extensions that must be enabled first?**
- **`pgcrypto`** — required (provides `gen_random_uuid()` used by `threads_gateway_keys` and `threads_post_metric_snapshots` in 013, and by 001/002). Each migration that needs it runs `create extension if not exists pgcrypto;` itself, so **no separate pre-step is required** — but the SQL editor role must have privilege to create extensions (on Supabase hosted, the postgres/service role does). Standard Supabase extensions (`uuid-ossp` etc.) are not required.

**Environment-specific values?**
- **NO schema values.** Migrations contain no project URLs, no keys, no account names, no hostnames. `002`'s header comment names the production project (`ztoxaksrdbcgceyulzwm`) but that is a comment, not executed SQL. All environment-specific config lives in `~/.threads-operator/accounts/<key>.env`, not in the schema.

## 4. Post-migration verification (prove schema completeness)

Run these against the fresh database after applying the 14 files in order. Expected results assume a clean apply.

1. **Object presence — 14 expected operator tables exist:**
   `threads_account_insights_snapshots`, `threads_post_insights_snapshots`, `threads_daily_rollups`, `threads_post_daily_rollups`, `threads_activity_events`, `threads_publish_queue`, `threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`, `threads_engagement_queue`, `threads_own_reply_engagement`.
   Via PostgREST OpenAPI: each returns HTTP 200 (not 404/PGRST205) on `GET /rest/v1/<table>?limit=0` with the service-role key. **This is exactly the check `scripts/doctor.sh` automates** (its 12-table probe + the 2 engagement tables).

2. **View present:** `threads_follows_from_post_summary` returns 200.

3. **FK integrity:**
   - `threads_engagement_queue.trend_candidate_id → threads_trend_candidates(id)`
   - `threads_trend_candidates.used_in_queue_id → threads_publish_queue(id)`
   - `threads_inbound_replies.parent_thread_id → threads_posts(thread_id)`
   - `threads_post_metric_snapshots.thread_id → threads_posts(thread_id)`
   Verify via `pg_constraint` (contype='f') — expect ≥4 FK rows across these tables.

4. **Critical CHECK constraints present:** `threads_trend_candidates_status_check` (must list `backlog`/`discarded` — proves 012 ran), `threads_engagement_approval_check`, `threads_own_reply_approval_check`. Verify via `pg_get_constraintdef`.

5. **Idempotency proof (replay safety):** re-apply all 14 files a second time — must complete with **zero errors** (every guard: `if not exists`, `drop ... if exists`, `to_regclass` DO blocks). This single step proves both correctness and replay-safety.

6. **RLS + privilege posture:** for each operator table, `relrowsecurity = true` where RLS is declared (004, 003, 002, 007, 001b, **and the 7 tables secured by 014**), and `service_role` holds the documented grants while `anon`/`authenticated` hold none (006/014 pattern). `tests/test_migration_014_security_reconcile.py` asserts 014 carries this posture so it cannot silently regress.

7. **Application smoke (strongest end-to-end proof):** `./scripts/doctor.sh --account <test>` against the fresh DB → the Supabase connectivity + schema block reports **PASS, zero missing tables**, exit 0 (account credentials aside). Then `.venv/bin/python -m pytest -q` (schema-independent unit suite, 557 tests) to confirm code/schema agreement.

## Status / next gate

- Branch `feat/plug-and-play-hardening` remains at the verified state; **no production Supabase changes were made by this handoff.**
- Production Supabase was **not** modified.

### 2026-09-23 update — external fresh-replay verification + 014 remediation

Independent verification (user-performed) against a throwaway Supabase project:
- **Production:** `002_post_daily_rollups.sql` applied successfully — the `rebuild_rollups.py` blocker is cleared.
- **Fresh DB:** all 14 migrations (`013,001,001b,002,003,004,005,006,007,008,009,010,011,012`) applied cleanly, and idempotency re-run passed with zero errors. View, 4 FKs, and the `backlog`/`discarded` CHECK all verified present.
- **Defect found:** the fresh DB did NOT reproduce production's RLS posture. RLS stayed **disabled** on `threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`, `threads_own_reply_engagement`, and Supabase Security Advisor reported **ERROR: threads_own_reply_engagement is public but RLS is not enabled** (anon/authenticated also held privileges on it). Root cause: 013 revokes but never enables RLS; 010 creates `threads_own_reply_engagement` with no grant/revoke/RLS section.
- **Remediation:** `migrations/014_fresh_schema_security_reconcile.sql` (new, additive, idempotent) enables RLS + revokes anon/authenticated + grants service_role (per-table, mirroring 013) + creates an idempotent `service_role_all_*` policy on each of the 7 tables. Apply it **last**. Regression-guarded by `tests/test_migration_014_security_reconcile.py`.
- Fresh-install migration order is now `013,001,001b,002,003,004,005,006,007,008,009,010,011,012,014`. Re-replay on the throwaway project to confirm the Security Advisor ERROR clears before merging to main.
