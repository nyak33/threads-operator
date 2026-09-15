# Threads Operator Multi-Account Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `threads-operator` portable to a fresh VPS/Hermes installation and safe for multiple Threads accounts, while preserving Insights and Activity and adding an account-scoped Supabase publishing queue.

**Architecture:** Keep one shared Python package and select one account explicitly for each command. Account secrets live in `~/.threads-operator/accounts/<key>.env`; the selected account constructs its own Threads API, browser profile, and Supabase store. Shared-database mutable rows are scoped by `account_key`.

**Tech Stack:** Python 3.11+, httpx, websockets, argparse, Supabase/PostgREST, Threads Graph API, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-15-multi-account-runtime-design.md`

## Global Constraints

- One repository installation must support multiple account env files.
- Account-bound commands must require `--account` or `THREADS_ACCOUNT`; never guess.
- Account-scoped secrets must come from the selected account file, not unrelated global exported credentials.
- Auto-posting is disabled unless both `THREADS_POSTING_ENABLED=true` and `THREADS_EXECUTION_MODE=auto_post`.
- Activity/browser collection remains read-only and challenge-safe.
- No real credentials, cookies, browser profiles, passwords, or customer data may enter Git.
- Shared-database activity and publishing rows must be isolated by `account_key`.
- Existing Insights and Activity regression behavior must remain green.

---

### Task 1: Account Configuration and Isolation

**Files:**
- Create: `tests/test_account_config.py`
- Create: `src/threads_operator/account_config.py`
- Create: `accounts/example.env`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `AccountConfig`, `AccountConfigError`, `resolve_account_name()`, `load_account_config()`.
- `AccountConfig.values` contains the selected account env mapping only; helper properties expose required settings without printing secrets.

- [ ] **Step 1: Add failing tests** for valid key loading, `THREADS_ACCOUNT` fallback, invalid account keys, missing account files, missing required settings, and proof that process-level `THREADS_ACCESS_TOKEN`/Supabase secrets do not replace missing values in the selected account file.
- [ ] **Step 2: Open/refresh draft PR and verify GitHub Actions fails** because `threads_operator.account_config` does not exist yet.
- [ ] **Step 3: Implement the minimal account loader** with `THREADS_OPERATOR_HOME` override and strict account-key validation.
- [ ] **Step 4: Add safe account template and ignore rules** for local runtime account files/profiles.
- [ ] **Step 5: Verify CI passes the account-config tests and existing suite.**

### Task 2: Account-Scoped Activity Integration

**Files:**
- Create/modify: `tests/test_multi_account_activity.py`
- Modify: `src/threads_operator/activity_collector.py`
- Modify: `src/threads_operator/supabase_store.py`
- Modify: `migrations/003_activity_follow_events.sql`

**Interfaces:**
- `SupabaseStore(..., account_key: str | None = None)` remains backwards-compatible.
- `insert_activity_events()` injects the configured `account_key` and deduplicates on `(account_key, fallback_fingerprint)`.
- Activity matching receives owned posts from the selected account's official Threads API in the unified CLI, avoiding a shared unscoped `threads_posts` lookup.

- [ ] **Step 1: Add failing tests** proving two accounts may store the same fallback fingerprint independently and that Activity persistence writes `account_key`.
- [ ] **Step 2: Verify the new tests fail for the expected missing account scoping.**
- [ ] **Step 3: Update store and migration** for account-scoped Activity rows and summary view.
- [ ] **Step 4: Keep legacy store construction working** for existing Insights tests.
- [ ] **Step 5: Verify Activity + Insights regression tests pass.**

### Task 3: Threads Text Publishing Primitive

**Files:**
- Create/modify: `tests/test_threads_publishing.py`
- Modify: `src/threads_operator/threads_api.py`

**Interfaces:**
- Produces: `create_text_container(text: str, reply_to_id: str | None = None) -> str`.
- Produces: `publish_container(creation_id: str) -> str`.
- Produces: `publish_text(text: str, reply_to_id: str | None = None) -> str` with bounded retry for transient publish-readiness/rate/server failures.

- [ ] **Step 1: Add failing tests** for text container request shape, reply parent request shape, publish request shape, empty returned IDs, and bounded transient retry.
- [ ] **Step 2: Verify RED in GitHub Actions.**
- [ ] **Step 3: Implement publishing primitives** without changing existing read-only Insights methods.
- [ ] **Step 4: Verify new and existing Threads API tests are green.**

### Task 4: Account-Scoped Supabase Publish Queue

**Files:**
- Create: `tests/test_publish_store.py`
- Modify: `src/threads_operator/supabase_store.py`
- Create: `migrations/004_threads_publish_queue.sql`

**Interfaces:**
- `peek_due_post(table, campaign_code=None)` reads the oldest due approved row for `self.account_key`.
- `claim_due_post(table, campaign_code=None)` conditionally changes one eligible row from `approved` to `posting` and returns it only if this worker won the claim.
- `mark_post_main_published()`, `mark_post_posted()`, and `mark_post_failed()` preserve state for reconciliation.

- [ ] **Step 1: Add failing tests** proving every queue read/claim includes `account_key`, campaign filtering is optional, conditional PATCH includes `status=eq.approved`, and losing a claim returns `None`.
- [ ] **Step 2: Verify RED.**
- [ ] **Step 3: Implement queue methods and migration** with safe RLS/service-role grants and account-key indexes.
- [ ] **Step 4: Verify queue tests and store regressions pass.**

### Task 5: Deterministic Publisher

**Files:**
- Create: `tests/test_publisher.py`
- Create: `src/threads_operator/publisher.py`

**Interfaces:**
- Produces: `publish_next(api, store, table, campaign_code=None, dry_run=False) -> dict`.
- Dry-run peeks only and never claims/posts.
- Normal execution claims first, posts main text, persists main ID, posts optional replies sequentially, then marks posted.
- Failures retain known IDs and mark the queue row `failed` with a concise error.

- [ ] **Step 1: Add failing tests** for dry-run no mutation, claim-before-publish, lost-claim no publish, main-only success, reply-chain success, and failure after main publication retaining the main post ID.
- [ ] **Step 2: Verify RED.**
- [ ] **Step 3: Implement minimal publisher orchestration.**
- [ ] **Step 4: Verify publisher/store/API tests pass together.**

### Task 6: Unified Multi-Account CLI and Doctor

**Files:**
- Create: `tests/test_operator_cli.py`
- Create: `src/threads_operator/operator_cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Console script: `threads-operator = threads_operator.operator_cli:main`.
- Commands: `accounts list`, `doctor`, `insights`, `activity-follow`, `publish`.
- Account-bound commands resolve exactly one `AccountConfig`.

- [ ] **Step 1: Add failing CLI tests** for account selection, list ordering, doctor secret-safe output, publishing safety gate, and routing of selected account settings to Insights/Activity/Publisher.
- [ ] **Step 2: Verify RED.**
- [ ] **Step 3: Implement CLI and local doctor.**
- [ ] **Step 4: Activity command obtains owned posts from the selected account's Threads API and passes them to the collector.**
- [ ] **Step 5: Publishing command refuses live execution unless both posting safety switches are enabled; dry-run remains allowed.**
- [ ] **Step 6: Verify CLI plus full regression suite passes.**

### Task 7: Fresh-VPS Bootstrap and Hermes Runbook

**Files:**
- Create: `scripts/bootstrap.sh`
- Create: `scripts/add_account.sh`
- Create: `tests/test_deployment_contract.py`
- Create: `GOAL.md`
- Create: `RUNBOOK.md`
- Create: `PROGRESS.md`
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- `scripts/bootstrap.sh` is idempotent and installs the package into `.venv`.
- `scripts/add_account.sh <key>` creates a mode-600 placeholder env file without overwriting existing credentials.
- Runbook gives Hermes deterministic install/add-account/doctor/run instructions.

- [ ] **Step 1: Add failing deployment-contract tests** asserting console entrypoint, bootstrap/add-account scripts, safe defaults, no overwrite behavior in script text, and required runbook/account-template references.
- [ ] **Step 2: Verify RED.**
- [ ] **Step 3: Add bootstrap/account scripts and root operator docs.**
- [ ] **Step 4: Update README and legacy `.env.example` to direct new deployments to account env files.**
- [ ] **Step 5: Verify deployment tests and secret scan pass.**

### Task 8: Final Review and Integration

**Files:**
- Review all changed files.

- [ ] **Step 1: Run GitHub Actions on the final branch and require success.**
- [ ] **Step 2: Inspect the final PR diff for account leakage, unsafe posting defaults, broad unrelated refactors, secrets, and migration inconsistencies.**
- [ ] **Step 3: Check CI job output and combined status.**
- [ ] **Step 4: Merge the PR to `main` only after the final green run, because the user explicitly authorized committing the completed setup to GitHub.**
- [ ] **Step 5: Fetch the merged commit and report the exact delivered behavior, remaining deployment-only steps (real credentials, browser login/profile, DB migration application), and any known limitations.**
