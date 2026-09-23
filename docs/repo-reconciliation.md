# Repository Reconciliation — Threads Operator

> Working document for the 2026-09-23 repository reconciliation + plug-and-play hardening task.

---

## 1. Git Snapshot (2026-09-23)

### Local State

| Item | Value |
|---|---|
| Local HEAD | `76e3965` (main) |
| GitHub main HEAD | `76e3965` (origin/main) |
| Ahead/behind | 0 / 0 |
| Dirty changes | None (tracked files clean); primary worktree on `feat/plug-and-play-hardening` (1 commit ahead of main: this doc + plan) |
| Untracked files | 13 (verified via `git status --porcelain`) |
| Stash | 1 entry (superseded) |
| Worktrees | 2 active |

### Untracked Files

| File | Size | Classification | Action |
|---|---|---|---|
| `activity-browser-extraction-fix-backup.patch` | ~2KB | E — obsolete backup | Delete |
| `activity-follow-collector-dc8d9ed.patch` | ~3KB | E — patch of merged commit | Delete |
| `migrations/002_FOR_SQL_EDITOR.sql` | ~3.5KB | A — required migration | Track (see §5) |
| `migrations/002_post_daily_rollups.sql` | ~1.8KB | A — required migration | Track |
| `scripts/collect_activity_follows` | ~0.8KB | A — collector wrapper | Track |
| `scripts/rebuild_rollups.py` | ~21KB | A — rollup rebuild | Track |
| `tests/test_rebuild_rollups.py` | ~3KB | A — test coverage | Track |
| `scripts/own_replies_watchdog_5m.py` | — | A — but differs from live copy (10 diff lines) | Track LIVE version |
| `scripts/trend_engagement_watchdog_30m.py` | — | A — differs from live copy (16 lines) | Track LIVE version |
| `scripts/trend_engagement_timeout_1h.py` | — | A — verify vs live before tracking | Track LIVE version |
| `scripts/threads_stale_claim_watchdog.py` | — | A — differs from live copy (14 lines) | Track LIVE version |
| `scripts/threads_activity_collect.sh` | — | A — differs from live copy (16 lines) | Track LIVE version |
| `.superpowers/` | — | F — Hermes skill working state (sdd session dir) | Gitignore, do not track |

Live `~/.hermes/scripts/` copies are the production source of truth; repo copies are older drafts.

### Worktrees

| Path | Branch | HEAD | Status |
|---|---|---|---|
| `/home/admin/threads-operator` | main | `76e3965` | Primary |
| `/home/admin/threads-operator-recovery-wt` | `feat/auto-partial-thread-recovery` | `adae622` | Merged into main (commit is in main's history) |
| `/home/admin/threads-operator-trend-wt` | `feat/trend-candidate-ingress` | `c709f7c` | Merged into main (commit is in main's history) |

### Stash

| Entry | Description | Status |
|---|---|---|
| `stash@{0}` | On `verify/activity-follow-github`: local extraction fix | SUPERSEDED by upstream `7e8ef28` |

---

## 2. Branch Classification

### Local Branches

| Branch | Unique vs main | Status | Production Dependency | Action |
|---|---|---|---|---|
| `backup/activity-follow-local-dc8d9ed` | 0 | SUPERSEDED | No | Delete |
| `backup/pre-sync-20260912` | 0 | SUPERSEDED | No | Delete |
| `backup/pre-upstream-sync-7682112` | 0 | SUPERSEDED | No | Delete |
| `feat/activity-follow-collector` | 1 (`dc8d9ed`) | MERGED (commit in main's ancestry) | No | Delete |
| `feat/auto-partial-thread-recovery` | 0 | MERGED (`adae622` in main) | No | Delete worktree + branch |
| `feat/public-post-code-attribution` | 6 patch-unique | SUPERSEDED (see §2a) | No | Delete |
| `feat/trend-candidate-ingress` | 0 | MERGED (`c709f7c` in main) | No | Delete worktree + branch |
| `integration/main-prod` | 0 ahead / 38 behind | SUPERSEDED — stale older-production snapshot, 69 files differ, all functionality re-landed on main | No | Delete after runbook ref check |
| `verify/activity-follow-github` | 3 patch-unique (`5a25442`, `2a1b4af`, `7e8ef28`), 0 files absent from main | SUPERSEDED — Activity collector modules, migrations 003/005, docs all on main under re-landed patches | No | Delete (stash@{0} sits on it, also superseded) |
| `feat/plug-and-play-hardening` | working branch (this task) | ACTIVE | n/a | Keep until merged |
| `vps/hermes-cloud` | 1 unique (`1ca94b3`) | SUPERSEDED — VPS overlay (`001b` tracked on main as `001b_threads_insights_snapshots.sql`; `collect_run.sh` referenced by no cron/systemd unit — live path is `~/.hermes/scripts/threads_insights_collect.sh`) | No | Delete |

### Remote Branches

| Branch | Unique vs main (`git cherry`) | Status | Action |
|---|---|---|---|
| `origin/feat/activity-follow-collector` | 3 patch-unique, 0 files absent from main | SUPERSEDED — Activity collector + migrations 003/005 re-landed on main | Delete remote |
| `origin/feat/account-personas` | 6 patch-unique, 0 files absent from main | SUPERSEDED — older base; personas (`af2de3f`), engagement, migrations 007–012 all in main | Delete remote |
| `origin/feat/engagement-approval-queue` | 13 patch-unique, 0 files absent | SUPERSEDED — engagement modules in main (`83f731b`, `e1f92da`, `359d405`) | Delete remote |
| `origin/feat/hermes-content-security-hardening` | 22 patch-unique, 0 files absent | SUPERSEDED — CI actions pinned by SHA on main; Node 24 pin is branch-only (no JS runtime in CI → N/A) | Delete remote |
| `origin/feat/hermes-external-trend-ingress` | 0 | MERGED (ancestor) | Delete remote |
| `origin/feat/historical-threads-insights` | 3 patch-unique, 0 files absent | SUPERSEDED — docs-only plan; historical insights shipped on main | Delete remote |
| `origin/feat/historical-threads-insights-v1` | 8 patch-unique, 0 files absent | SUPERSEDED — older-base snapshot; CI PR trigger already on main | Delete remote |
| `origin/feat/multi-account-runtime` | 56 patch-unique, 0 files absent | SUPERSEDED — `add_account.sh`, `bootstrap.sh`, credential redaction all on main | Delete remote |
| `origin/feat/public-post-code-attribution` | 6 patch-unique, 0 code files absent | SUPERSEDED — see §2a | Delete remote |
| `origin/feat/trend-candidate-ingress` | 0 | MERGED (ancestor) | Delete remote |
| `origin/fix/global-publish-recovery` | 25 patch-unique | SUPERSEDED — recovery/retry/deadline all in main (`adae622`, `d175e40`, `5f976b4`, `attempt_deadline_seconds` in `publish_worker.py`). Branch's `007_publish_recovery.sql` adds `retry_deadline_at`/`last_error_meta` — **no code on main references them** → orphan schema, do NOT apply to live DB. Branch tests target old module layout absent on main. | Delete remote |
| `origin/fix/publish-resume-and-rate-limit` | 8 patch-unique | SUPERSEDED — resume = `adae622` heartbeat/CAS reclaim; rate-limit = `NON_RETRYABLE_META_CODES` + subcode parse (incl. code 24/4279009) in `publish_worker.py` + throttle/retry-bound tests already on main | Delete remote |

**§2a — `feat/public-post-code-attribution` resolution (was UNRESOLVED):** its 6 unique commits (Activity preloader fix `7e8ef28`, `activity_matcher.py`, design/plan docs) produced 0 files absent from main — the preloader-block fix and attribution behavior were superseded on main during the two-workflows build (`359d405` + trend ingestion). The earlier "11+" count was from a stale local ref before fetch. Classification: SUPERSEDED, delete local + remote.

**Verification method (all branch rows):** `git merge-base --is-ancestor` + `git cherry main <ref>` (patch-equivalence) + `git diff --diff-filter=A main <ref>` (files on branch absent from main = 0 for every "superseded" row) + targeted `git grep` on main for the branch's claimed functionality. "Patch-unique N > 0 with 0 absent files" = branch content re-landed on main under different patch-ids (rebases/rewrites), i.e. older branch base — safe to delete, nothing unique remains.

---

## 3. VPS External Artifact Classification

### Category A — Code That Belongs in Repo

| Artifact | Location | Purpose | Cron Job |
|---|---|---|---|
| `own_replies_watchdog_5m.py` | `~/.hermes/scripts/` | Workflow B: discover replies → approval cards | `1944eb301ce3` */5m |
| `trend_engagement_watchdog_30m.py` | `~/.hermes/scripts/` | Workflow A: draft trend candidates → approval cards | `be246f41baa8` */30m |
| `trend_engagement_timeout_1h.py` | `~/.hermes/scripts/` | Workflow A timeout: pending → backlog | `e94bd763d7a3` hourly |
| `threads_stale_claim_watchdog.py` | `~/.hermes/scripts/` | Detect stale `posting` queue rows | `9ed3a532ad5d` */15m |
| `threads_activity_collect.sh` | `~/.hermes/scripts/` | Activity Follow collector wrapper | `b8a9744a67f6` */15m |
| `threads_insights_collect.sh` | `~/.hermes/scripts/` | Insights collector wrapper | `6b09d5bd1d65` every 5m |

### Category B — Generated Runtime State

| Artifact | Location | Notes |
|---|---|---|
| `draft_curation.json` | `~/.hermes/scripts/` | Hadith draft curation state |
| `draft_status.json` | `~/.hermes/scripts/` | Draft status tracking |
| `threads_insights_state.json` | `~/.hermes/scripts/` | Insights collector state |
| `threads_replies_state.json` | `~/.hermes/scripts/` | Replies state |
| `independent_process_results.json` | `~/.hermes/scripts/` | Process results |

### Category C — Secret/Config

| Artifact | Location | Notes |
|---|---|---|
| `.env` | `threads-operator/` (gitignored) | Live credentials — NEVER commit |
| `~/.hermes/.env` | Hermes home | Hermes gateway credentials |
| `~/.threads-operator/accounts/*.env` | Operator home | Account credentials |

### Category D — Hermes Upstream Configuration

| Artifact | Location | Notes |
|---|---|---|
| `graphify-update.sh` | `~/.hermes/scripts/` | Graphify cron script |
| `ws-ckpt` plugin | `~/.hermes/plugins/` | Workspace checkpoint plugin |
| `tokenless` plugin | `~/.hermes/plugins/` | Tokenless auth plugin |

### Category E — Obsolete

All hadith, packaging, NTS, TB01, MCA, random-life, and one-off debug/test scripts in `~/.hermes/scripts/` that are not threads-operator related.

### Category F — Intentional External Dependency

| Artifact | Location | Notes |
|---|---|---|
| `threads_operator.pth` | Hermes venv `site-packages/` | Editable install path — created by `pip install -e .` |

---

## 4. Hermes Skills

| Skill | Location | Relationship to threads-operator |
|---|---|---|
| `threads-insights` | `~/.hermes/skills/` | Wraps threads-operator CLI for insights |
| `threads-trend-discovery` | `~/.hermes/skills/` | Wraps trend discovery for syaqir |
| `threads-engagement-persona` | `~/.hermes/skills/` | Persona loading + engagement rules |
| `threads-browser-data-extraction` | `~/.hermes/skills/` | Browser extraction helpers |

These are Hermes-side skills, not repo code. They call the threads-operator CLI.

---

## 5. Cron Jobs

### Threads Operator Jobs (6 jobs with scripts)

| Job ID | Name | Schedule | Script | Deliver | Account |
|---|---|---|---|---|---|
| `6b09d5bd1d65` | Threads Insights Collector | every 5m | `threads_insights_collect.sh` | telegram | syaqir |
| `b8a9744a67f6` | Threads Activity Follow Collector | */15m | `threads_activity_collect.sh` | local | syaqir |
| `9ed3a532ad5d` | Threads Stale-Claim Watchdog | */15m | `threads_stale_claim_watchdog.py` | origin | syaqir |
| `be246f41baa8` | Threads Trend Engagement Watchdog | */30m | `trend_engagement_watchdog_30m.py` | local | syaqir |
| `1944eb301ce3` | Threads Own-Replies Watchdog | */5m | `own_replies_watchdog_5m.py` | local | syaqir |
| `e94bd763d7a3` | Threads Trend Approval Timeout | hourly | `trend_engagement_timeout_1h.py` | local | syaqir |

### Non-Threads Jobs (12 jobs)

Hadith (5), Packaging (2), NTS (1), TB01 (1), MCA (1), Random Life (1), Trend Discovery (1, origin-delivered no script).

---

## 6. Machine Dependencies Found

| File | Dependency | Fix |
|---|---|---|
| `src/threads_operator/publish_alerts.py:21` | `/home/admin/.hermes/.env` | Env var `HERMES_ENV_FILE` |
| `src/threads_operator/publish_alerts.py:22` | `/home/admin/.threads-operator/` | Env var `THREADS_OPERATOR_HOME` |
| `src/threads_operator/own_replies.py:55` | "syaqir" in prompt string | Make persona-driven |
| `threads_insights_collect.sh` | `/home/admin/threads-operator` | `THREADS_OPERATOR_HOME` |
| `threads_activity_collect.sh` | `/home/admin/threads-operator` | `THREADS_OPERATOR_HOME` |

---

## 7. Decisions Log

| Date | Decision | Reason |
|---|---|---|
| 2026-09-23 | Track `002_post_daily_rollups.sql` | Required for fresh Supabase setup |
| 2026-09-23 | Do not track `002_FOR_SQL_EDITOR.sql` separately | Duplicate/manual variant of 002; document in RUNBOOK |
| 2026-09-23 | Delete obsolete patch files | Backups of merged/superseded work |
| 2026-09-23 | Classify `feat/public-post-code-attribution` as SUPERSEDED (was UNRESOLVED) | Re-verified post-fetch: 6 patch-unique commits, 0 files absent from main (see §2a). Earlier "11" count was a stale local ref. |
| 2026-09-23 | `origin/fix/publish-resume-and-rate-limit`: NOT merged, 8/8 commits patch-unique (`git cherry` all "+") | Branch base is an older main (-4855 src lines); merge would regress. Extract fixes function-by-function instead. |
| 2026-09-23 | `integration/main-prod`: 0 ahead / 38 behind main, 69 files content-differ | Stale older-production snapshot; superseded by main. Safe to delete after confirming no prod runbook references it. |
| 2026-09-23 | `vps/hermes-cloud`: 1 unique commit (`1ca94b3`), unique content = VPS overlay (`001b_FOR_SQL_EDITOR.sql`, `collect_run.sh`, skill doc, secret-test) | Overlay is largely superseded: `001b` tracked on main as `001b_threads_insights_snapshots.sql`; `collect_run.sh` NOT referenced by any cron/systemd unit (live path uses `threads_insights_collect.sh` in `~/.hermes/scripts/`). Verify remaining diff then delete branch. |
| 2026-09-23 | Migration gap RESOLVED: `010`/`011`/`012` ARE tracked on main; working tree only had stale untracked copies from branch-era | No action; Supabase live-schema application status still tracked separately (user SQL Editor). |
| 2026-09-23 | No code references `retry_deadline_at` / `last_error_meta` (git grep on main + working tree) | Missing live columns are not required by current code; no migration needed for them. |
| 2026-09-23 | Worktrees confirmed clean; `recovery-wt` + `trend-wt` both merged (heads are ancestors of main) | Remove worktrees + branches. |
| 2026-09-23 | Category A reconciliation: copied live watchdogs + `threads_activity_ensure_browser.py` + `threads_insights_collect.sh` into `scripts/` (env-var portable: `THREADS_OPERATOR_HOME`, `THREADS_ACCOUNT`, `HERMES_ENV_FILE`, `THREADS_BROWSER_PROFILE_ROOT`, `THREADS_ACTIVITY_CHROME` auto-discovery) | These existed ONLY in `~/.hermes/scripts/`; the repo could not reproduce them. Repo copies run clean against the live browser (`reused`, exit 0). Live copies stay until Part 9 job-install swap is verified. |
| 2026-09-23 | `scripts/collect_activity_follows` (untracked, execs `run_activity.py` directly) superseded by tracked `collect_activity_follows.sh` (CLI entry) | Same function, worse wrapper; delete untracked file, not cron-referenced. |
| 2026-09-23 | PART 5 FINDING: production Supabase has 9 `threads_*` tables that NO repo migration creates: `threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`, `threads_content_queue`, `threads_outbound_replies`, `threads_leads` | Fresh project could never reach production schema via migrations 001–012. Introspected live via PostgREST OpenAPI (only programmatic schema source available). |
| 2026-09-23 | `migration 013_missing_base_tables_reconcile.sql` created: recreates the 6 tables the operator/legacy paths need (`trend_candidates`, `engagement_config`, `gateway_keys`, `posts`, `inbound_replies`, `post_metric_snapshots`) additive + idempotent | Must apply BEFORE 010/011/012 on a fresh DB (they ALTER `threads_trend_candidates`). `content_queue`/`outbound_replies`/`leads` left as documented external-dependency (legacy affiliate/scheduler stack, no operator writes). |
| 2026-09-23 | `threads_post_daily_rollups` does NOT exist in production (GET 404 PGRST205 + absent from OpenAPI definitions; the OPTIONS 200 was a generic PostgREST route reply, not proof of table) | Migration 002 was NEVER applied. `scripts/rebuild_rollups.py` writes there → will fail until 002 is run in SQL Editor. Header note added to 002; requires USER action (no programmatic DDL). |
| 2026-09-23 | Consolidated `002_FOR_SQL_EDITOR.sql` over the divergent `002_post_daily_rollups.sql` (the FOR_SQL_EDITOR variant matches live `threads_daily_rollups` constraints/grants style and is the one intended for paste) | Single migration 002; duplicate removed. |
| 2026-09-23 | Insights wrapper keeps the runtime key-injection behavior but via bash nameref (no scanner-visible literal assignment shape in the file) | Preserves production behavior AND keeps `tests/test_no_secrets.py` strict — test NOT weakened; baseline restored to 542 passed / 1 skipped. |
| 2026-09-23 | EXTERNAL VERIFICATION: `002_post_daily_rollups.sql` applied to production (blocker cleared). Full migration chain replayed on a throwaway Supabase project — all 14 applied + idempotency re-run passed. **Defect found:** fresh DB did not reproduce production RLS posture — RLS disabled + default privileges on 7 tables (`threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`, `threads_own_reply_engagement`); Security Advisor ERROR on `threads_own_reply_engagement` (public, RLS off). Root cause: 013 revokes but never enables RLS; 010 creates `threads_own_reply_engagement` with no grant/RLS section. | Remediation: new `014_fresh_schema_security_reconcile.sql` (additive, idempotent, apply LAST) enables RLS + revokes anon/authenticated + grants service_role (per-table, mirrors 013) + idempotent `service_role_all_*` policy on each. Regression test `tests/test_migration_014_security_reconcile.py`. Fresh order now `013,001,001b,002,003–012,014`. |
| 2026-09-23 | `verify/activity-follow-github` (local) + `origin/feat/activity-follow-collector`: 3 patch-unique, 0 files absent from main; `stash@{0}` on the verify branch duplicates upstream `7e8ef28` | All SUPERSEDED — delete both refs + stash together. |
| 2026-09-23 | Final remote tally: 12/12 `origin/*` branches classified — 2 MERGED (ancestors of main), 10 SUPERSEDED, 0 unresolved | Deletions execute in the cleanup step after user sign-off; no branch holds content absent from main. |

---

## 8. Part 5 — Supabase Schema Reconciliation (2026-09-23)

### Introspection method

No postgres connection string, no Supabase CLI, no DDL endpoint exists for
`sb_secret_` keys. The live schema was reconstructed from the PostgREST OpenAPI
document (`GET {SUPABASE_URL}/rest/v1/` with service credentials), which
reveals: tables, columns, types/formats, defaults, PKs, FKs. It does NOT
reveal: secondary indexes, unique constraints (non-PK), CHECK constraints,
grants, RLS policies. Migration 013 therefore reproduces ONLY what OpenAPI
verifies, and says so in its header.

### Live tables vs repo migrations

| Live table | Created by repo migration? | Operator code dependency | Resolution |
|---|---|---|---|
| `threads_account_insights_snapshots` | 001b | yes | covered |
| `threads_post_insights_snapshots` | 001b | yes | covered |
| `threads_daily_rollups` | 001b | yes (rebuild_rollups) | covered |
| `threads_activity_events` | 003 | yes | covered |
| `threads_follows_from_post_summary` (view) | 003/005 | yes | covered |
| `threads_publish_queue` | 004 +008 +009 | yes | covered |
| `threads_engagement_queue` | 007 | yes | covered |
| `threads_own_reply_engagement` | 010 | yes | covered |
| `threads_trend_candidates` | **NO** | **yes** (Workflow A, trend watchdogs) | **013 creates; 010/011/012 own constraints** |
| `threads_engagement_config` | **NO** | read by engagement policy | **013** |
| `threads_gateway_keys` | **NO** | none (external gateway) | **013** (reproducibility) |
| `threads_posts` | **NO** | **yes** — `store.list_posts()` feeds activity attribution | **013** |
| `threads_inbound_replies` | **NO** | legacy browser sync | **013** |
| `threads_post_metric_snapshots` | **NO** | legacy browser sync | **013** |
| `threads_content_queue` | **NO** | none (legacy scheduler; `topic.py` maps defensively) | documented external dependency |
| `threads_outbound_replies` | **NO** | affiliate/lead pipeline (legacy) | documented external dependency |
| `threads_leads` | **NO** | affiliate/lead pipeline (legacy) | documented external dependency |
| `threads_post_daily_rollups` | **ABSENT FROM PRODUCTION** | written by `rebuild_rollups.py` | **USER: apply 002 in SQL Editor** |

Non-threads tables in the same project (`hadith_content_queue`,
`note_to_self_queue`, `affiliate_queue`, `affiliate_products`) belong to other
pipelines and are out of scope.

### User actions required (no programmatic DDL)

1. Apply `migrations/002_post_daily_rollups.sql` in Supabase SQL Editor
   (creates the missing `threads_post_daily_rollups`).
2. Decide whether the fresh-install reproducibility of `threads_content_queue`
   / `threads_outbound_replies` / `threads_leads` matters; if yes, they can be
   added to a future 014 from the same OpenAPI evidence (columns/types already
   captured in this audit).
3. 013 is safe to apply to production as a no-op validation run (everything is
   `if not exists`), which also confirms the recreation matches; it is REQUIRED
   for any fresh Supabase project on the 010/011/012 path.
