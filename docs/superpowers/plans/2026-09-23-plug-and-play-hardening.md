# Threads Operator Repository Reconciliation + Plug-and-Play Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the threads-operator GitHub repository into the canonical, reproducible source of truth for the system currently running on this VPS — preserving working behavior, reconciling all live code, documenting honestly, and hardening toward plug-and-play installation.

**Architecture:** Preserve-first. Audit live state, reconcile into Git, document accurately, remove machine dependencies, improve portability. No redesign of working components. Refactor only for portability/security/correctness/reproducibility.

**Tech Stack:** Python 3.11, Hermes Agent, Supabase (Postgres), Threads Graph API, Telebot, bash, pytest.

**Spec:** User /goal message (2026-09-23) — 23 parts covering snapshot, reconciliation, VPS audit, preservation, Supabase reproducibility, multi-account, hidden dependencies, Hermes integration, cron, README (7 sections), docs, bootstrap, doctor, testing, clean-room, commit/push, final report.

## Global Constraints

- Do NOT commit secrets or credentials.
- Do NOT destructively modify production data.
- Do NOT force-push shared history.
- Do NOT change working production cron until replacement behavior is verified.
- Do NOT claim plug-and-play until fresh-install verification proves it.
- Do NOT list unimplemented features as current in README.
- Do NOT treat "plug-and-play" as permission to redesign working code.
- Baseline tests: 542 passed, 1 skipped. Must stay green.
- Model/provider: kimi-k3 / custom. Unchanged.
- Cron jobs: 18 active. Unchanged until explicitly modified.

## Review Focus

1. **Live code outside GitHub** — Hermes scripts (watchdogs, executors, collectors) that production depends on but aren't in the repo. Must be classified and reconciled.
2. **Branch drift** — 8 local + 10 remote branches with unclear merge status. Must classify each: MERGED / SUPERSEDED / STILL REQUIRED / PARTIALLY REQUIRED / OBSOLETE / UNRESOLVED.
3. **Untracked migrations** — `002_FOR_SQL_EDITOR.sql` and `002_post_daily_rollups.sql` are untracked but may be required for fresh Supabase setup.
4. **Hardcoded machine dependencies** — `/home/admin/.hermes/.env` in `publish_alerts.py`, `/home/admin/threads-operator` in `threads_insights_collect.sh`, account-specific strings.
5. **Multi-account claims vs reality** — README/GOAL claim multi-account support; must verify what's actually implemented vs documented.

---

## Task 1: Preserve Snapshot + Create Reconciliation Doc

**Files:**
- Create: `docs/repo-reconciliation.md`

**Interfaces:**
- Consumes: Git state from snapshot (already collected)
- Produces: `docs/repo-reconciliation.md` — the working document for all reconciliation decisions

- [ ] **Step 1: Write reconciliation doc with snapshot data**

Document:
- Local HEAD: `76e3965` (main, up to date with origin/main)
- GitHub main HEAD: `76e3965`
- Ahead/behind: 0/0
- Dirty changes: none (only untracked files)
- Local-only commits: none on main
- Worktrees: 2 active (`threads-operator-recovery-wt` @ `adae622`, `threads-operator-trend-wt` @ `c709f7c`)
- Stash: 1 entry (superseded)
- Untracked: 7 files (patches, migrations, scripts, tests)
- Branches: 8 local, 10 remote

- [ ] **Step 2: Classify all branches**

For each branch, determine: unique commits, important files, equivalent in main, production dependency, action.

- [ ] **Step 3: Commit**

```bash
git add docs/repo-reconciliation.md
git commit -m "docs: repo reconciliation snapshot and branch classification"
```

---

## Task 2: Audit Live VPS — Classify External Artifacts

**Files:**
- Modify: `docs/repo-reconciliation.md`

**Interfaces:**
- Consumes: Live VPS state (Hermes scripts, cron, .pth, skills)
- Produces: Classification of every external artifact (A-F)

- [ ] **Step 1: Classify Hermes scripts**

Category A (code that belongs in repo):
- `own_replies_watchdog_5m.py` — Workflow B watchdog, production cron
- `trend_engagement_watchdog_30m.py` — Workflow A watchdog, production cron
- `trend_engagement_timeout_1h.py` — timeout watchdog, production cron
- `threads_stale_claim_watchdog.py` — stale-claim watchdog, production cron
- `threads_activity_collect.sh` — Activity collector wrapper, production cron
- `threads_insights_collect.sh` — Insights collector wrapper, production cron

Category B (generated runtime state):
- `draft_curation.json`, `draft_status.json`, `threads_insights_state.json`, `threads_replies_state.json`, `independent_process_results.json`

Category C (secret/config):
- `.env` (in repo but gitignored), `~/.hermes/.env`, `~/.threads-operator/accounts/*.env`

Category D (Hermes upstream config):
- `graphify-update.sh`, `ws-ckpt` plugin, `tokenless` plugin

Category E (obsolete):
- `byd_watchdog.py`, `post_reply.py`, `fix_state.py`, `fetch_threads.py`, `find-gateway.ps1`, `gw-action.ps1`, `gw-health.ps1`, `inspect-gateway.ps1`, `restart-gateway.ps1`, `start-gateway.ps1`, `analyze_drafts.py`, `analyze_fails.py`, `analyze_remaining.py`, `approved_updater.py`, `bulk_strip_approve.py`, `check_approved_format.py`, `check_approved.py`, `check_draft_schema.py`, `check_schema.py`, `curate_drafts.py`, `debug_patch.py`, `debug_remaining.py`, `debug_supa.py`, `extract_hadith_content.py`, `extract_jakim_sources.py`, `final_verify.py`, `fresh_verify.py`, `hadith_asar.py`, `hadith_isyak.py`, `hadith_maghrib.py`, `hadith_subuh.py`, `hadith_zohor.py`, `mca_affiliate_executor_5m.py`, `nts_executor.py`, `packaging_executor_6pm.py`, `packaging_executor_9am.py`, `process_independent.py`, `process_remaining.py`, `push_approvals.py`, `quality_filter.py`, `random_life_executor_5m.py`, `show_drafts_raw.py`, `show_state.py`, `sync_insights.py`, `sync_insights_full.sh`, `sync_insights_incremental.sh`, `tb01_executor.py`, `test_38_46_67.py`, `test_38.py`, `test_extraction.py`, `test_final_fixes.py`, `test_final_pipeline.py`, `test_final.py`, `test_patch_fields.py`, `test_raw_data.py`, `test_split.py`, `threads_activity_ensure_browser.py`, `threads_insights.py`, `threads_replies.py`, `tnb_collector_wrapper.py`, `verify_hadith.py`

Category F (intentional external dependency):
- `~/.hermes/hermes-agent/venv/lib/python3.11/site-packages/threads_operator.pth` — editable install path

- [ ] **Step 2: Document cron jobs**

18 active jobs. Classify which belong to threads-operator:
- `6b09d5bd1d65` Threads Insights Collector (threads_insights_collect.sh)
- `b8a9744a67f6` Threads Activity Follow Collector (threads_activity_collect.sh)
- `9ed3a532ad5d` Threads Stale-Claim Watchdog (threads_stale_claim_watchdog.py)
- `be246f41baa8` Threads Trend Engagement Watchdog (trend_engagement_watchdog_30m.py)
- `1944eb301ce3` Threads Own-Replies Watchdog (own_replies_watchdog_5m.py)
- `e94bd763d7a3` Threads Trend Approval Timeout Watchdog (trend_engagement_timeout_1h.py)
- `d1f1c0b7a01b` Threads Trend Discovery (origin-delivered, no script)

Others (hadith, packaging, NTS, TB01, MCA, random-life) are separate projects.

- [ ] **Step 3: Document Hermes skills**

- `threads-insights` — skill for insights collection
- `threads-trend-discovery` — skill for trend discovery
- `threads-engagement-persona` — skill for engagement persona
- `threads-browser-data-extraction` — skill for browser extraction

These are Hermes-side skills, not repo code. Document as Category D/F.

- [ ] **Step 4: Commit**

```bash
git add docs/repo-reconciliation.md
git commit -m "docs: VPS external artifact classification"
```

---

## Task 3: Reconcile Category A Code Into Repo

**Files:**
- Create: `scripts/own_replies_watchdog_5m.py`
- Create: `scripts/trend_engagement_watchdog_30m.py`
- Create: `scripts/trend_engagement_timeout_1h.py`
- Create: `scripts/threads_stale_claim_watchdog.py`
- Create: `scripts/threads_activity_collect.sh`
- Create: `scripts/threads_insights_collect.sh`
- Modify: `docs/repo-reconciliation.md`

**Interfaces:**
- Consumes: Live scripts from `~/.hermes/scripts/`
- Produces: Repo copies with machine dependencies removed

- [ ] **Step 1: Copy watchdog scripts with path normalization**

Replace hardcoded paths:
- `Path.home() / ".hermes" / "projects" / "threads"` → `Path(os.environ.get("THREADS_PROJECT_HOME", Path.home() / ".hermes" / "projects" / "threads"))`
- `Path.home() / "threads-operator"` → `Path(os.environ.get("THREADS_OPERATOR_HOME", Path.cwd()))`
- `/home/admin/threads-operator` → `THREADS_OPERATOR_HOME` env or repo-relative

- [ ] **Step 2: Copy collector shell scripts with path normalization**

- `threads_activity_collect.sh`: replace `cd /home/admin/threads-operator` with `cd "${THREADS_OPERATOR_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"`
- `threads_insights_collect.sh`: same, plus replace `/home/admin/threads-operator/.env` with `${THREADS_OPERATOR_HOME}/.env`

- [ ] **Step 3: Verify scripts are syntactically valid**

```bash
python -m py_compile scripts/own_replies_watchdog_5m.py
python -m py_compile scripts/trend_engagement_watchdog_30m.py
python -m py_compile scripts/trend_engagement_timeout_1h.py
python -m py_compile scripts/threads_stale_claim_watchdog.py
bash -n scripts/threads_activity_collect.sh
bash -n scripts/threads_insights_collect.sh
```

- [ ] **Step 4: Commit**

```bash
git add scripts/own_replies_watchdog_5m.py scripts/trend_engagement_watchdog_30m.py scripts/trend_engagement_timeout_1h.py scripts/threads_stale_claim_watchdog.py scripts/threads_activity_collect.sh scripts/threads_insights_collect.sh docs/repo-reconciliation.md
git commit -m "feat: reconcile live watchdog and collector scripts into repo"
```

---

## Task 4: Reconcile Untracked Files

**Files:**
- Create: `migrations/002_post_daily_rollups.sql` (track it)
- Create: `scripts/rebuild_rollups.py` (track it)
- Create: `tests/test_rebuild_rollups.py` (track it)
- Delete: `activity-browser-extraction-fix-backup.patch` (obsolete — backup of superseded work)
- Delete: `activity-follow-collector-dc8d9ed.patch` (obsolete — patch of already-merged dc8d9ed)
- Modify: `docs/repo-reconciliation.md`

**Interfaces:**
- Consumes: Untracked files from snapshot
- Produces: Tracked migrations/scripts/tests, deleted obsolete patches

- [ ] **Step 1: Track migration 002**

`002_post_daily_rollups.sql` is additive-only, safe to track. `002_FOR_SQL_EDITOR.sql` is a duplicate/manual variant — document it in reconciliation doc but don't track separately.

- [ ] **Step 2: Track rebuild_rollups.py + test**

Already in repo directory but untracked. Verify test passes, then track.

```bash
.venv/bin/python -m pytest tests/test_rebuild_rollups.py -v
```

- [ ] **Step 3: Delete obsolete patches**

These are backups of work already in git history. Remove.

- [ ] **Step 4: Commit**

```bash
git add migrations/002_post_daily_rollups.sql scripts/rebuild_rollups.py tests/test_rebuild_rollups.py docs/repo-reconciliation.md
git rm activity-browser-extraction-fix-backup.patch activity-follow-collector-dc8d9ed.patch
git commit -m "feat: track migration 002, rebuild_rollups, and tests; remove obsolete patches"
```

---

## Task 5: Branch Classification and Reconciliation

**Files:**
- Modify: `docs/repo-reconciliation.md`

**Interfaces:**
- Consumes: Branch audit data
- Produces: Final classification + action per branch

- [ ] **Step 1: Classify local branches**

| Branch | Unique commits | Status | Action |
|---|---|---|---|
| `backup/activity-follow-local-dc8d9ed` | 0 vs feat/activity-follow-collector | SUPERSEDED | Delete |
| `backup/pre-sync-20260912` | 0 vs main | SUPERSEDED | Delete |
| `backup/pre-upstream-sync-7682112` | 0 vs main | SUPERSEDED | Delete |
| `feat/activity-follow-collector` | 1 (`dc8d9ed`) | MERGED into main? Check | If merged, delete; if not, verify |
| `feat/auto-partial-thread-recovery` | 0 vs main | MERGED (commit `adae622` is in main's history) | Delete worktree + branch |
| `feat/public-post-code-attribution` | 11 | UNRESOLVED — docs + schema + matcher code not in main | Evaluate: is this needed? |
| `feat/trend-candidate-ingress` | 0 vs main | MERGED (commit `c709f7c` is in main's history) | Delete worktree + branch |
| `integration/main-prod` | ? | Check vs main | Classify |
| `vps/hermes-cloud` | ? | Check vs main | Classify |

- [ ] **Step 2: Classify remote branches**

| Branch | Unique commits | Status | Action |
|---|---|---|---|
| `origin/feat/account-personas` | 5 docs commits | PARTIALLY REQUIRED — persona docs may already be in main | Verify |
| `origin/feat/engagement-approval-queue` | 5 | MERGED? Check if `568953b` etc in main | Verify |
| `origin/feat/hermes-content-security-hardening` | 5 | MERGED? Check | Verify |
| `origin/feat/hermes-external-trend-ingress` | 0 | MERGED | Delete remote |
| `origin/feat/historical-threads-insights` | 3 | Check if in main | Verify |
| `origin/feat/historical-threads-insights-v1` | 5 | Check if in main | Verify |
| `origin/feat/multi-account-runtime` | 5 | Check if in main | Verify |
| `origin/feat/public-post-code-attribution` | 11+ | Same as local | Same action |
| `origin/feat/trend-candidate-ingress` | 5 | Check if in main | Verify |
| `origin/fix/global-publish-recovery` | 5 | Check if in main | Verify |
| `origin/fix/publish-resume-and-rate-limit` | 5 | Check if in main | Verify |

- [ ] **Step 3: Execute branch cleanup (safe ones only)**

Delete branches confirmed merged or superseded. Keep unresolved ones with documentation.

- [ ] **Step 4: Commit**

```bash
git add docs/repo-reconciliation.md
git commit -m "docs: branch classification and cleanup plan"
```

---

## Task 6: Supabase Reproducibility

**Files:**
- Modify: `docs/repo-reconciliation.md`
- Modify: `RUNBOOK.md`

**Interfaces:**
- Consumes: Migration files 001-012, untracked 002
- Produces: Verified migration ordering, documented manual steps

- [ ] **Step 1: Verify migration ordering**

Migrations present: 001, 001b, 002 (untracked), 003-012. Check for gaps or ordering issues.

- [ ] **Step 2: Document manual SQL steps**

`002_FOR_SQL_EDITOR.sql` is marked "PASTE KE SUPABASE SQL EDITOR" — document this in RUNBOOK as a manual step with reason.

- [ ] **Step 3: Verify no missing historical changes**

Check if any production schema state isn't covered by migrations.

- [ ] **Step 4: Commit**

```bash
git add docs/repo-reconciliation.md RUNBOOK.md
git commit -m "docs: Supabase migration ordering and manual steps"
```

---

## Task 7: Multi-Account Architecture Documentation

**Files:**
- Modify: `README.md`
- Modify: `GOAL.md`

**Interfaces:**
- Consumes: Code inspection of account_config.py, CLI account handling
- Produces: Accurate multi-account documentation

- [ ] **Step 1: Verify what's implemented**

Check `account_config.py` for account isolation, `operator_cli.py` for `--account` flag, Supabase store for account scoping.

- [ ] **Step 2: Document reality vs claims**

- Implemented: account file contract, `--account` CLI flag, account-scoped queue table, account-scoped personas
- Partially implemented: browser profile isolation (per-account dir but shared CDP port logic), engagement account scoping
- Future work: per-account cron job isolation, per-account rate limit tracking

- [ ] **Step 3: Commit**

```bash
git add README.md GOAL.md
git commit -m "docs: accurate multi-account implementation status"
```

---

## Task 8: Remove Hidden Machine Dependencies

**Files:**
- Modify: `src/threads_operator/publish_alerts.py`
- Modify: `src/threads_operator/own_replies.py`

**Interfaces:**
- Consumes: Hardcoded paths found in audit
- Produces: Configurable/env-based paths

- [ ] **Step 1: Fix publish_alerts.py**

Replace `/home/admin/.hermes/.env` with env var `HERMES_ENV_FILE` defaulting to `~/.hermes/.env`.

- [ ] **Step 2: Fix own_replies.py**

Remove hardcoded "syaqir" reference; make persona-driven.

- [ ] **Step 3: Run tests**

```bash
.venv/bin/python -m pytest tests/ -q
```

- [ ] **Step 4: Commit**

```bash
git add src/threads_operator/publish_alerts.py src/threads_operator/own_replies.py
git commit -m "fix: remove hardcoded machine dependencies"
```

---

## Task 9: Hermes Integration Documentation

**Files:**
- Modify: `README.md`
- Create: `docs/architecture.md`

**Interfaces:**
- Consumes: .pth file, skills, cron integration
- Produces: Documented integration path

- [ ] **Step 1: Document .pth integration**

`threads_operator.pth` points to `/home/admin/threads-operator/src`. This is an editable install artifact. Document that `pip install -e .` creates this automatically.

- [ ] **Step 2: Document skills relationship**

Hermes skills (`threads-insights`, `threads-trend-discovery`, `threads-engagement-persona`, `threads-browser-data-extraction`) are Hermes-side wrappers that call the threads-operator CLI. Document that they live in `~/.hermes/skills/` and are installed separately.

- [ ] **Step 3: Document cron integration**

Hermes cron runs scripts from `~/.hermes/scripts/` or from repo. Document the current state and the target state (scripts in repo, cron referencing repo paths).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/architecture.md
git commit -m "docs: Hermes integration architecture"
```

---

## Task 10: Cron / Runtime Jobs Documentation

**Files:**
- Modify: `RUNBOOK.md`
- Modify: `docs/repo-reconciliation.md`

**Interfaces:**
- Consumes: Cron job list, script locations
- Produces: Documented job inventory + install process

- [ ] **Step 1: Document current jobs**

For each threads-operator job: name, schedule, script, account, purpose, logs, failure behavior.

- [ ] **Step 2: Design idempotent install process**

Create `scripts/install_jobs.sh` that:
- Checks if job already exists (by name)
- Creates/updates Hermes cron job pointing to repo script
- Sets correct workdir and deliver target
- Is idempotent (safe to re-run)

- [ ] **Step 3: Document uninstall**

`scripts/uninstall_jobs.sh` that removes threads-operator cron jobs by name pattern.

- [ ] **Step 4: Commit**

```bash
git add RUNBOOK.md docs/repo-reconciliation.md scripts/install_jobs.sh scripts/uninstall_jobs.sh
git commit -m "docs: runtime job inventory and idempotent install process"
```

---

## Task 11: README — Product Overview

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: All verified capabilities from code inspection
- Produces: Complete README sections 1-10

- [ ] **Step 1: Write What It Does**

Content queue → scheduler/executor → Threads API → engagement/replies → analytics.

- [ ] **Step 2: Write Current Capabilities**

Verified:
- Content Management: draft ingress, topic/tag, character limit, main+replies, series
- Publishing: scheduled, retries, rate-limit, failure recovery, status tracking
- Engagement: own-post reply monitoring, drafting, approval flow, Telegram controls
- Trend Discovery: candidate collection, scoring, approval queue, backlog, timeout
- Content Generation: provider abstraction, persona handling
- Analytics: insights collection, historical data
- Multi-Account: partial (document exactly)
- Telegram: approval cards, buttons
- Automation: watchdogs, executors, schedules

- [ ] **Step 3: Write Known Limitations**

Platform: no external-root replies (keyword_search own-only, GET /{external_id} 400 subcode 33), rate limits, token expiry, approval requirements.
Implementation: multi-account partial, some setup manual, cron-dependent features.

- [ ] **Step 4: Write How It Works**

ASCII architecture diagram.

- [ ] **Step 5: Write Requirements**

Runtime: Python 3.11+, Supabase project, Threads app + token, Telegram bot (optional).
Dev/Optional: Graphify, Diagram Design, Superpowers (explicitly NOT runtime).

- [ ] **Step 6: Write Installation**

Current: Manual/semi-automated. Target: Plug-and-play. Document current procedure.

- [ ] **Step 7: Write Configuration**

Categories: Threads/Meta, Supabase, Telegram, model/provider, account config, personas, schedules. Classify REQUIRED/OPTIONAL/GENERATED.

- [ ] **Step 8: Write Operations**

Start, stop, status, logs, test, doctor, add account, install jobs, remove jobs, update, rollback.

- [ ] **Step 9: Write Current Features vs Roadmap**

Current: verified list. Roadmap: Near Term (plug-and-play hardening), Next (reliability), Later (intelligence), Future (optional).

- [ ] **Step 10: Commit**

```bash
git add README.md
git commit -m "docs: README product overview, capabilities, limitations, architecture, requirements, install, config, operations, roadmap"
```

---

## Task 12: Project Status Documents

**Files:**
- Modify: `GOAL.md`
- Modify: `PROGRESS.md`
- Modify: `RUNBOOK.md`

**Interfaces:**
- Consumes: Current state from reconciliation
- Produces: Updated status docs

- [ ] **Step 1: Update GOAL.md**

Reflect reconciliation and plug-and-play hardening as current milestone.

- [ ] **Step 2: Update PROGRESS.md**

Add reconciliation done, branch cleanup, script reconciliation, machine dependency removal.

- [ ] **Step 3: Update RUNBOOK.md**

Add fresh-install verification section, update job installation, add doctor section.

- [ ] **Step 4: Commit**

```bash
git add GOAL.md PROGRESS.md RUNBOOK.md
git commit -m "docs: update GOAL, PROGRESS, RUNBOOK with current state"
```

---

## Task 13: Bootstrap / Plug-and-Play Hardening

**Files:**
- Modify: `scripts/bootstrap.sh`
- Create: `scripts/doctor.sh`

**Interfaces:**
- Consumes: Existing bootstrap.sh, doctor CLI command
- Produces: Improved bootstrap, standalone doctor script

- [ ] **Step 1: Improve bootstrap.sh**

Add checks: git available, python 3.11+, pip, venv creation, dependency install, directory setup.

- [ ] **Step 2: Create doctor.sh**

Wrapper around `threads-operator doctor` that checks:
- Python/runtime
- Dependencies
- Environment/config
- Account config
- Threads credentials presence
- Supabase connectivity
- Expected database schema
- Telegram config
- Hermes integration
- Runtime directories
- Jobs/cron
- Provider/model config

Never prints secrets.

- [ ] **Step 3: Commit**

```bash
git add scripts/bootstrap.sh scripts/doctor.sh
git commit -m "feat: improve bootstrap, add doctor script"
```

---

## Task 14: Testing

**Files:**
- Modify: `docs/fresh-install-verification.md` (create)

**Interfaces:**
- Consumes: All changes from Tasks 1-13
- Produces: Test results, clean-room verification

- [ ] **Step 1: Run regression tests**

```bash
python -m compileall -q src scripts
.venv/bin/python -m pytest tests/ -q
git diff --check
```

Expected: 542 passed, 1 skipped.

- [ ] **Step 2: Clean-room test**

```bash
cd /tmp
git clone https://github.com/nyak33/threads-operator.git threads-operator-clean
cd threads-operator-clean
bash scripts/bootstrap.sh
bash scripts/add_account.sh testaccount
.venv/bin/threads-operator doctor --account testaccount
```

Verify: no hardcoded /home/admin, imports work, CLI works, doctor runs.

- [ ] **Step 3: Document results**

Write `docs/fresh-install-verification.md` with clean-room test output.

- [ ] **Step 4: Commit**

```bash
git add docs/fresh-install-verification.md
git commit -m "docs: fresh install verification results"
```

---

## Task 15: Final Review + Push

**Files:**
- Modify: `docs/repo-reconciliation.md`
- Modify: `docs/superpowers-pilot.md`

**Interfaces:**
- Consumes: All previous tasks
- Produces: Final reconciliation state, pilot task #1 record

- [ ] **Step 1: Final whole-branch review**

Review all changes. Check for:
- No secrets committed
- No hardcoded machine paths
- Tests still green
- Documentation accurate
- No broken references

- [ ] **Step 2: Update superpowers-pilot.md**

Record as REAL PILOT TASK #1 with measurements:
- What Superpowers materially improved
- Unnecessary ceremony
- Graphify contribution
- Wrong assumptions avoided
- Rework
- User intervention
- Scope discipline
- Verification outcome

- [ ] **Step 3: Push to GitHub**

```bash
git push origin feat/plug-and-play-hardening
```

- [ ] **Step 4: Final report**

Produce THREADS OPERATOR REPOSITORY + PLUG-AND-PLAY REPORT.
