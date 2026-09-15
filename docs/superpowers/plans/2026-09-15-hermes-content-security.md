# Hermes Draft Ingress and Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic draft-ingress path for optional Hermes-generated content while hardening credential destinations, database grants, and CI supply-chain checks.

**Architecture:** Hermes owns all LLM/provider credentials and generation. Threads Operator accepts already-generated text through an account-scoped `enqueue-draft` command and writes it to the existing Supabase queue as `draft`. Security changes restrict Threads API destinations, tighten database grants, and harden CI without changing the default publishing workflow.

**Tech Stack:** Python 3.11+, argparse, httpx, Supabase REST/PostgREST, PostgreSQL migrations, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-15-hermes-content-security-design.md`

## Global Constraints

- No LLM provider API key may be read, stored, or required by Threads Operator.
- Draft ingress must never approve or publish content.
- Every draft write must be scoped to the explicitly selected account.
- Threads access tokens may only be sent to HTTPS `graph.threads.net`.
- Existing production migrations must be upgraded forward rather than relying on edits to already-applied SQL.
- Live posting remains disabled by default.

---

### Task 1: Draft ingress contract

**Files:**
- Modify: `tests/test_publish_store.py`
- Modify: `tests/test_operator_cli.py`
- Modify: `src/threads_operator/supabase_store.py`
- Modify: `src/threads_operator/operator_cli.py`

**Interfaces:**
- Produces: `SupabaseStore.enqueue_draft(table, main_post_text, reply_texts=None, campaign_code=None, scheduled_at=None) -> dict`
- Produces CLI: `threads-operator enqueue-draft --account <key> --text <text> [--reply <text>]... [--campaign-code <code>] [--scheduled-at <iso>]`

- [ ] Add a store test asserting the POST payload includes selected `account_key`, `status=draft`, main text, replies, optional campaign code, and never `approved`.
- [ ] Run the targeted test and confirm it fails because `enqueue_draft` does not exist.
- [ ] Add a CLI test asserting `enqueue-draft` uses the selected account store and does not construct/call Threads publishing API.
- [ ] Run the targeted CLI test and confirm it fails because the command does not exist.
- [ ] Implement the minimum store method using the existing validated queue URL and service-role headers.
- [ ] Implement the CLI parser/handler and return a non-secret JSON result.
- [ ] Run the targeted tests and confirm they pass.

### Task 2: Restrict Threads token destination

**Files:**
- Modify: `tests/test_threads_api.py`
- Modify: `src/threads_operator/threads_api.py`

**Interfaces:**
- `ThreadsAPI(..., base_url=...)` accepts only HTTPS URLs whose hostname is exactly `graph.threads.net`.

- [ ] Add tests that reject `http://graph.threads.net`, `https://evil.example`, and `https://graph.threads.net.evil.example`.
- [ ] Run the targeted tests and confirm they fail under current behavior.
- [ ] Add URL parsing/validation in `ThreadsAPI.__init__` before the client can send credentials.
- [ ] Keep the official default URL working.
- [ ] Run the targeted tests and confirm they pass.

### Task 3: Database and CI hardening

**Files:**
- Create: `migrations/006_security_hardening.sql`
- Modify: `tests/test_migration_security.py`
- Modify: `.github/workflows/tests.yml`
- Modify: `pyproject.toml`

**Interfaces:**
- Migration revokes `anon`/`authenticated` privileges from operator Insights tables and grants required rights to `service_role` only.
- CI has read-only contents permission, immutable action pins, compile/test, and dependency audit.

- [ ] Extend migration security tests to require a forward hardening migration with explicit revokes.
- [ ] Confirm the test fails while migration `006` is absent.
- [ ] Add the migration using idempotent revoke/grant statements.
- [ ] Pin checkout/setup-python actions to immutable SHAs and set `permissions: contents: read`.
- [ ] Add `pip-audit` to the dev toolchain and execute it in CI after dependency installation.
- [ ] Run migration tests and full tests in CI.

### Task 4: Deployment documentation and contract

**Files:**
- Modify: `README.md`
- Modify: `RUNBOOK.md`
- Modify: `GOAL.md`
- Modify: `PROGRESS.md`
- Modify: `accounts/example.env` only if clarification is required; do not add LLM provider variables.

**Interfaces:**
- Documents two generation modes: external/ChatGPT default and optional Hermes-owned LLM generation.
- Documents global Hermes LLM secret ownership vs per-account Threads/Supabase credential ownership.

- [ ] Document the optional generation flow and `enqueue-draft` example.
- [ ] State explicitly that no LLM API key belongs in Threads Operator/account examples.
- [ ] Document shared-Supabase personal mode and separate-Supabase isolation mode.
- [ ] Add migration `006` to fresh/upgrade instructions.
- [ ] Recommend required CI checks/branch protection for `main` as a repository setting.
- [ ] Run secret-scan tests and full CI.

### Final Verification

- [ ] `python -m compileall -q src scripts`
- [ ] `python -m pytest -q`
- [ ] `python -m pip_audit`
- [ ] Inspect the PR diff for accidental credentials or provider coupling.
- [ ] Merge only after CI is green.
