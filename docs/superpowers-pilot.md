# Superpowers Pilot — Real Pilot Task #1 — COMPLETE (2026-09-24)

**Task:** Threads Operator — repository reconciliation + product documentation + plug-and-play roadmap.
**Branch:** `feat/plug-and-play-hardening` (merged to main as `85b4eb5`; branch since cleaned up)
**Date:** 2026-09-23

> **Final status (2026-09-24):** All Supabase migration actions closed — `002_post_daily_rollups` and `014_fresh_schema_security_reconcile` both applied to production and externally verified; full chain replayable from zero on a fresh project. Branch/worktree/stash cleanup signed off and executed. Canonical state: main `7a424f0`, clean tree, 598 passed / 1 skipped, doctor healthy, 7/7 runtime jobs healthy.

This is the first real (non-synthetic) task executed under the Superpowers workflow. Record per the pilot requirement: what Superpowers materially improved, where it added ceremony, and the verification outcome.

## What Superpowers materially improved

- **Structured reconciliation over ad-hoc cleanup.** The A–F artifact classification (code-belongs-in-repo / runtime-state / secret-config / Hermes-config / obsolete / intentional-external) prevented two failure modes: committing secrets, and deleting live production scripts that cron still points at. Live `~/.hermes/scripts/` copies were left running until the repo replacement is verified.
- **Evidence-before-claims discipline.** The Supabase schema audit was driven by PostgREST OpenAPI introspection rather than assumptions. That surfaced the single biggest reproducibility finding: 9 production `threads_*` tables (including actively-read `threads_trend_candidates` and `threads_posts`) had **no** creating migration — a fresh project could never have reached production schema via 001–012. The resulting `013_missing_base_tables_reconcile.sql` was written from verified evidence only, with invented DDL (checks/indexes/defaults OpenAPI cannot see) explicitly removed after an honesty review.
- **Clean-room verification as a first-class step.** Part 22 was not a formality: it caught two real defects that live-VPS testing had masked (missing git exec bits on the three scripts a fresh operator runs first; a doctor.sh false-PASS where schema completeness was claimed alongside total Supabase probe failure).
- **Test-weakening guardrail.** When `tests/test_no_secrets.py` failed against a ported script, the fix was a bash nameref in the script to satisfy the strict scanner — not a relaxed test. The "do not weaken tests to pass" rule held; the suite grew 542 → 565 with zero tests loosened.
- **Independent external replay as the final gate.** The strongest validation did not come from in-repo testing at all: the user replayed the full migration chain against a brand-new throwaway Supabase project. That surfaced a fresh-install security defect invisible to code review and the live-VPS audit — see "Externally discovered defect" below.

## Unnecessary ceremony

- The full branch-by-branch merge/retire classification produced mostly SUPERSEDED branches whose unique commits were already in main; the audit value was confirmation, not recovery. Worth doing once for a repo with hidden worktrees, but the per-branch "unique commits / files absent / equivalent-in-main" matrix was heavier than the outcome required.
- The A–F taxonomy has 6 buckets; in practice everything resolved to A (port it), C (secret — keep out), or E (obsolete). B/D/F were theoretically useful but had few members.

## Graphify contribution

- Not used as a runtime dependency and not needed for the reconciliation itself. The dependency/code-structure questions this task raised (which modules import which env keys, where account config flows) were answerable directly from the small, well-factored `src/threads_operator/` package. Graphify remains a documentation/planning tool, correctly kept out of the runtime requirements list.

## Wrong assumptions avoided

- Assumed the live `rebuild_daily_rollups.py` had drifted from the repo's `rebuild_rollups.py` — verified false: the script is repo-only; the real issue was the absent production table, not code drift.
- Assumed `hermes cron` jobs could be listed as JSON for the installer — false (`hermes cron list` is table-only); the installer was rewritten to match against the durable `~/.hermes/cron/jobs.json` store instead of a nonexistent machine-readable CLI path.
- Assumed doctor could claim schema completeness from table-missing data alone — false; probe errors (e.g. placeholder credentials) must suppress the PASS. Fixed.
- Assumed the migration set was reproducible-complete once 013 closed the base-table gap and the chain applied cleanly — **false for the security posture.** The external fresh replay proved the schema (tables/FKs/CHECKs/view) reproduced, but the RLS/grant posture did not. Applying cleanly ≠ reproducing production security.

## Externally discovered defect (2026-09-23) + remediation

**Discovery:** after the chain was deemed "code-complete," the user provisioned a throwaway Supabase project and replayed all 14 migrations in dependency order (`013,001,001b,002,003,004,005,006,007,008,009,010,011,012`). Results: every migration applied; idempotency re-run passed with zero errors; `threads_follows_from_post_summary` view, 4 expected FKs, and the `backlog`/`discarded` CHECK all verified present. `002` was also confirmed applied to production (the `rebuild_rollups.py` blocker cleared).

**Defect:** the fresh DB did **not** reproduce production's Row-Level Security posture. RLS stayed **disabled** on `threads_trend_candidates`, `threads_engagement_config`, `threads_gateway_keys`, `threads_posts`, `threads_inbound_replies`, `threads_post_metric_snapshots`, `threads_own_reply_engagement`, and Supabase Security Advisor reported **ERROR: `threads_own_reply_engagement` is public but RLS is not enabled** (anon/authenticated also held privileges on it). Root cause: `013` revokes client roles but never enables RLS; `010` creates `threads_own_reply_engagement` with no grant/revoke/RLS section, so it inherited permissive defaults.

**Why in-repo testing missed it:** the defect is a *database-runtime security posture* gap, not a code or schema-object gap. No unit test, compile check, or clean-room CLI run touches a live Postgres `relrowsecurity` flag. Only an actual replay against real Postgres exposes it — which is exactly why the throwaway-project step (initially deferred as "manual/console") was load-bearing.

**Remediation:** `migrations/014_fresh_schema_security_reconcile.sql` — new, additive, idempotent, applies LAST. Per affected table it enables RLS, revokes `public`/`anon`/`authenticated`, grants `service_role` the exact privileges 013 established (and S/I/U for `threads_own_reply_engagement`), and creates an idempotent `service_role_all_<table>` policy. No permissive client-facing policies added — the operator remains service_role-only. On production it is posture-preserving (RLS already enabled there); it only adds the service_role policy where absent. Regression-guarded by `tests/test_migration_014_security_reconcile.py` (8 tests asserting RLS/revoke/grant/policy per table so it cannot silently regress). Fresh-install order is now `013,001,001b,002,003–012,014`.

**Lesson for the workflow:** "reproducible system" must include the **security posture**, not just object existence. A replay test should assert RLS flags and role grants, not only table/FK presence. The replay gate is now a permanent pre-merge requirement, and 014 exists precisely because the first replay was honest enough to report a red Security Advisor flag rather than declare victory at "14/14 applied."

## Rework

- `scripts/install_jobs.sh` was written twice: first version relied on `hermes cron list --json` and had shell-escaping bugs in schedule extraction; rewritten around a single embedded-Python planner emitting a shell-safe action plan. The rewrite is strictly better (one pass, no per-job subprocess storms, quoted-field handling).
- `scripts/doctor.sh` schema-PASS logic fixed post-clean-room (see above).

## User intervention

- Required and received: none for code. User-only actions:
  1. ~~Apply `migrations/002_post_daily_rollups.sql` to production~~ — DONE (user, 2026-09-23; blocker cleared).
  2. ~~Provide a throwaway Supabase project for the full migration replay~~ — DONE (user, 2026-09-23; replay executed, surfaced the RLS defect).
  3. ~~Apply `migrations/014_fresh_schema_security_reconcile.sql` to production + re-replay the chain incl. 014 on the throwaway project to confirm the Security Advisor ERROR clears~~ — DONE (user, 2026-09-23; both verified, ERROR cleared, 014 idempotent and recorded in production migration history).
  4. ~~Sign off on old remote branch/worktree/stash deletions~~ — DONE (user sign-off received; cleanup executed 2026-09-24 — obsolete fix/feat branches deleted, one worktree, no stashes, only `main` + 3 intentional `backup/*` refs remain).

## Scope discipline

- "Preserve first, portable second, refactor only when necessary" held. No working component was rewritten for aesthetics. The only refactors were portability-driven (env-var overrides for `/home/admin`, parse-not-source env loading for the watchdogs, `THREADS_OPERATOR_REPO` override naming collision fix).
- Old branches were classified, **not** blindly merged. Obsolete deletions were limited to artifacts pre-classified as obsolete in the decisions log.

## Verification outcome

- Test suite: **565 passed, 1 skipped** (baseline 542 + 15 contract tests + 8 migration-014 security regression tests; none weakened). `python -m compileall -q src scripts` clean; `git diff --check` clean.
- Clean room: fresh clone → bootstrap → CLI → 557 tests → account scaffold → doctor (accurate PASS/WARN/FAIL) → runtime-job status. Two defects found and fixed in-repo.
- Live doctor: 11 pass / 1 warn / 0 fail (the warn was the pending 002 — now resolved in production).
- Runtime jobs: 7/7 present in the live Hermes store and matching the manifest.
- External migration replay (user): 14/14 applied + idempotency re-run zero errors; surfaced the fresh-install RLS defect → remediated with 014. **014 re-replay verified (2026-09-23):** full chain `013→014` from zero on the throwaway project passed, 014 idempotent, Security Advisor ERROR cleared; 014 also applied to production and recorded in migration history.
- **Task COMPLETE (2026-09-24).** All Supabase migration actions are closed (002 and 014 applied to production and externally verified). Branch/worktree/stash cleanup signed off and executed 2026-09-24. Canonical state: main `7a424f0` (== `origin/main`), clean working tree, 598 passed / 1 skipped, doctor healthy, 7/7 runtime jobs healthy, one worktree, no stashes. Plug-and-play status: **READY**.
