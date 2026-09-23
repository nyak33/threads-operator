# Fresh-Install Verification (Part 22)

Date: 2026-09-23
Branch: `feat/plug-and-play-hardening`
Commit under test: `d326c5b` (+ uncommitted doctor.sh false-PASS fix, same day)
Method: `git clone /home/admin/threads-operator /tmp/cleanroom/threads-operator` — no inherited VPS files, no live operator home, no live Supabase writes. No production posting performed.

## Results

| Check | Result | Evidence |
|---|---|---|
| Clone carries branch + history | PASS | `git branch --show-current` → `feat/plug-and-play-hardening`, HEAD `d326c5b` |
| `./scripts/bootstrap.sh` executes | PASS (after fix) | First clone FAILED: `Permission denied` — `bootstrap.sh`, `add_account.sh`, `publish_queue_worker_hermes.py` were tracked `100644`. Fixed to `100755` in commit `d326c5b`; re-clone bootstrap then completed: venv created, `threads-operator 0.2.0` installed, operator home scaffolded. |
| CLI present + importable | PASS | `.venv/bin/threads-operator --help` lists all 11 subcommands (accounts, doctor, insights, activity-follow, enqueue-draft, publish, publish-worker, engagement, trend, trend-engagement, own-replies) |
| Test suite in clean clone | PASS | `557 passed, 1 skipped` (pytest installed into clone venv; identical to live suite) |
| No hardcoded `/home/admin` in src/scripts/config/migrations | PASS | `grep -rn "/home/admin"` → zero hits in `src/ scripts/ config/ migrations/ accounts/`. Remaining hits are docs only: `docs/publish-queue-worker.md:62` and `RUNBOOK.md:193` (example `hermes cron create` lines — fixed separately), and historical records (`docs/repo-reconciliation.md`, `docs/superpowers/plans/*`) which legitimately document what was found on this VPS. |
| `doctor.sh` failure modes | PASS | With isolated `THREADS_OPERATOR_HOME=/tmp/cleanroom/op-home` and no account: exit 1, FAIL on missing account file, WARN on missing runtime dirs. With placeholder account `testacct`: FAIL listing the 3 missing required values. Live re-check unchanged: 11 pass / 1 warn / 0 fail. |
| `doctor.sh` Supabase error handling | PASS (after fix) | Placeholder credentials → all probes `URLError`. First version incorrectly also printed `PASS all 12 expected tables present` alongside the FAIL (schema-PASS branch didn't require error-free probes). Fixed: schema completeness is only claimed when probes are clean AND no table is missing. |
| `add_account.sh` in clean env | PASS | `THREADS_OPERATOR_HOME=/tmp/cleanroom/op-home ./scripts/add_account.sh testacct` → created `accounts/testacct.env` (mode 600, placeholder values), browser-profile dir (700), printed next-step instructions. |
| `install_jobs.sh --status` for a new account | PASS | Correctly reported the 3 global jobs present and the 4 account-scoped jobs MISSING for `testacct`; exit 1. Idempotent name-matching confirmed against the live Hermes store (read-only). |
| Runtime-job install in clean room | NOT EXERCISED | `--dry-run` verified convergence plan against live jobs; actual `--install` intentionally not run to avoid mutating the production Hermes cron store from a throwaway clone. The production store already matches the manifest (7/7 present, verified via `--status`). |
| Supabase migrations on a fresh project | EXTERNALLY VERIFIED — COMPLETE (2026-09-23) | User replayed the full chain **including 014** (`013,001,001b,002,003,004,005,006,007,008,009,010,011,012,014`) against a throwaway Supabase project: all applied cleanly; 014 re-run passed with zero errors (idempotency confirmed); all 7 affected tables have RLS enabled; anon/authenticated access removed; intended service_role policies present; previous Supabase Security Advisor ERROR **cleared**. Production: `002_post_daily_rollups.sql` and `014_fresh_schema_security_reconcile.sql` both applied — all 7 tables RLS-enabled, anon/authenticated grants zero, service_role policies present, `threads_own_reply_engagement` security error cleared, 014 recorded in production migration history, and the `rebuild_rollups.py` blocker cleared. |
| Real posting / browser automation | NOT EXERCISED | Explicitly out of scope for this test per goal constraints. |

## Defects found and fixed during verification

1. **Missing exec bits** on `bootstrap.sh`, `add_account.sh`, `publish_queue_worker_hermes.py` — fresh clone could not run the documented first command. Fixed in `d326c5b`.
2. **doctor.sh false PASS** — schema-completeness PASS printed even when all Supabase probes errored. Fixed same day (schema PASS now requires zero probe errors).
3. **Fresh-install RLS/privilege gap (externally discovered 2026-09-23).** The fresh replay did NOT reproduce production's Row-Level Security posture: RLS stayed disabled and default privileges remained on `threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`, `threads_own_reply_engagement`. Supabase Security Advisor reported **ERROR: `threads_own_reply_engagement` is public but RLS is not enabled**, and anon/authenticated held privileges on it. Root cause: `013` revokes client roles but never enables RLS; `010` creates `threads_own_reply_engagement` with no grant/revoke/RLS section. **Fix:** `migrations/014_fresh_schema_security_reconcile.sql` (additive, idempotent, apply LAST) enables RLS + revokes anon/authenticated + grants service_role (per-table, mirroring 013) + creates an idempotent `service_role_all_*` policy on each of the 7 tables. Regression-guarded by `tests/test_migration_014_security_reconcile.py`. Fresh-install order is now `013,001,001b,002,003,004,005,006,007,008,009,010,011,012,014`. **Verified fixed (2026-09-23):** re-replay of the full chain incl. 014 on the throwaway project passed, 014 idempotency re-run passed with zero errors, RLS enabled on all 7, anon/authenticated removed, service_role policies present, Security Advisor ERROR cleared. 014 was also applied to production with the same posture verified and is recorded in the production migration history.

## Verdict

A fresh clone can: bootstrap → get a working CLI → run the full test suite → create an account scaffold → run doctor (with accurate PASS/WARN/FAIL) → see which runtime jobs are missing. The path from "fresh VPS" to "configured operator" is code-complete up to the points that require credentials (Threads token, Supabase project). All previously manual steps are now closed: `002` and `014` are applied to production, and the full 15-file chain (013→014) is verified replayable from zero on a fresh project with the production security posture reproduced.
