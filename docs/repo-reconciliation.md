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
| Dirty changes | None (tracked files clean) |
| Untracked files | 7 |
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
| `feat/public-post-code-attribution` | 11 | UNRESOLVED | TBD | Evaluate — docs + schema + matcher code |
| `feat/trend-candidate-ingress` | 0 | MERGED (`c709f7c` in main) | No | Delete worktree + branch |
| `integration/main-prod` | ? | CHECK | TBD | Classify |
| `vps/hermes-cloud` | ? | CHECK | TBD | Classify |

### Remote Branches

| Branch | Unique vs main | Status | Action |
|---|---|---|---|
| `origin/feat/account-personas` | 5 docs | PARTIALLY REQUIRED | Verify if persona docs in main |
| `origin/feat/engagement-approval-queue` | 5 | CHECK | Verify merge status |
| `origin/feat/hermes-content-security-hardening` | 5 | CHECK | Verify merge status |
| `origin/feat/hermes-external-trend-ingress` | 0 | MERGED | Delete remote |
| `origin/feat/historical-threads-insights` | 3 | CHECK | Verify merge status |
| `origin/feat/historical-threads-insights-v1` | 5 | CHECK | Verify merge status |
| `origin/feat/multi-account-runtime` | 5 | CHECK | Verify merge status |
| `origin/feat/public-post-code-attribution` | 11+ | UNRESOLVED | Same as local |
| `origin/feat/trend-candidate-ingress` | 5 | CHECK | Verify merge status |
| `origin/fix/global-publish-recovery` | 5 | CHECK | Verify merge status |
| `origin/fix/publish-resume-and-rate-limit` | 5 | CHECK | Verify merge status |

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
| 2026-09-23 | Classify `feat/public-post-code-attribution` as UNRESOLVED | 11 commits of docs+schema+matcher not in main; needs evaluation |
