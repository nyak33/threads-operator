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
| Supabase migrations on a fresh project | NOT EXERCISED | Creating a second Supabase project is a manual/console step; migration SQL has not been replayed against an empty project. `013_missing_base_tables_reconcile.sql` ordering (before 010–012) is documented in the file header. Replay test remains an open verification gap (requires a throwaway Supabase project). |
| Real posting / browser automation | NOT EXERCISED | Explicitly out of scope for this test per goal constraints. |

## Defects found and fixed during verification

1. **Missing exec bits** on `bootstrap.sh`, `add_account.sh`, `publish_queue_worker_hermes.py` — fresh clone could not run the documented first command. Fixed in `d326c5b`.
2. **doctor.sh false PASS** — schema-completeness PASS printed even when all Supabase probes errored. Fixed same day (schema PASS now requires zero probe errors).

## Verdict

A fresh clone can: bootstrap → get a working CLI → run the full test suite → create an account scaffold → run doctor (with accurate PASS/WARN/FAIL) → see which runtime jobs are missing. The path from "fresh VPS" to "configured operator" is code-complete up to the points that require credentials (Threads token, Supabase project) and the two documented manual steps (002 rollups SQL apply; Supabase project creation).
