# Historical Threads Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first usable `threads-insights` subsystem: append-only historical account/post snapshots, deterministic growth/velocity analysis, configurable sampling, and a Hermes skill for operating it.

**Architecture:** A small Python package reads owned Threads data through the Threads Graph API and persists raw snapshots through a Supabase REST adapter. Pure analytics functions operate only on snapshot values so they can be tested without network access and recalculated later. A thin CLI wires environment/config to the collector.

**Tech Stack:** Python 3.11+, `httpx`, pytest, Postgres/Supabase SQL, Hermes `SKILL.md`.

**Spec:** `docs/superpowers/specs/2026-09-09-threads-operator-design.md`

## Global Constraints

- Public repository: no real API tokens, Supabase keys, account IDs, phone numbers, or customer data.
- Raw snapshots are append-only source of truth; derived values must be reproducible.
- Missing metrics remain unknown/null, never silently converted to measured zero.
- Analytics labels distinguish measured, calculated, inferred, and attributed evidence.
- No posting/replying is performed by this subsystem.
- Default post sampling intervals: 5m (0–2h), 15m (2–6h), 30m (6–24h), 60m (1–3d), 360m (3–7d), 1440m (>7d).
- Account snapshot interval default: 15m.
- Direct WhatsApp source convention may use `Hi, saya datang dari Threads.` with placeholder contact details only.

---

### Task 1: Foundation, secure configuration, and persistence schema

**Files:** `.gitignore`, `.env.example`, `config.example.yaml`, `pyproject.toml`, `migrations/001_threads_insights.sql`, `src/threads_operator/__init__.py`, `tests/test_no_secrets.py`.

**Produces:** required env contract and tables `threads_account_snapshots`, `threads_post_snapshots`, `threads_daily_rollups`.

- [x] Write repository-safety test first and verify it fails without `.env.example`.
- [x] Add placeholder-only env/config, package metadata, append-only SQL schema, and source package.
- [x] Verify safety test passes.

### Task 2: Deterministic sampling and analytics primitives

**Files:** `src/threads_operator/insights.py`, `tests/test_insights.py`.

**Produces:**
- `sample_interval_minutes(post_age_minutes)`
- `growth_rate(previous, current)`
- `velocity(previous_value, current_value, elapsed_minutes)`
- `detect_second_wave(points, min_acceleration_ratio=2.0)`
- evidence constants `MEASURED`, `CALCULATED`, `INFERRED`, `ATTRIBUTED`

- [x] Write failing age-boundary, missing-data, growth, velocity, and second-wave tests.
- [x] Implement minimal pure functions.
- [x] Verify tests pass.

### Task 3: Read-only Threads Graph API adapter

**Files:** `src/threads_operator/threads_api.py`, `tests/test_threads_api.py`.

**Produces:** `ThreadsAPI` with `list_posts`, `get_post_insights`, and `get_account_insights`.

- [x] Write failing `httpx.MockTransport` tests for owned posts and both supported Insights response shapes.
- [x] Implement read-only adapter; no publish endpoints.
- [x] Add failing test for a combined metric request rejected because one metric is unavailable.
- [x] Implement 400 fallback to individual metric reads while preserving unsupported metrics as `None`.
- [x] Verify adapter tests pass.

### Task 4: Supabase append-only snapshot adapter

**Files:** `src/threads_operator/supabase_store.py`, `tests/test_supabase_store.py`.

**Produces:** `SupabaseStore` with append-only inserts and latest account/post snapshot reads.

- [x] Write failing transport tests for REST paths, auth headers, ordering, and `limit=1`.
- [x] Implement minimal adapter without raw snapshot update/delete paths.
- [x] Verify tests pass.

### Task 5: Collector orchestration and CLI

**Files:** `src/threads_operator/collector.py`, `src/threads_operator/cli.py`, `scripts/collect_insights.py`, `tests/test_collector.py`, `tests/test_cli.py`.

**Produces:** `collect_once(api, store, now, ...)` and one-shot CLI.

- [x] Write failing tests for fresh sampling, due checks, partial post failure isolation, null metric preservation, and 15-minute account cadence.
- [x] Implement collector using age-aware intervals and account/post snapshot history.
- [x] Include `account_id` on both account and post raw snapshot rows.
- [x] Write failing CLI config tests and implement safe environment loading.
- [x] Verify collector/CLI tests pass.

### Task 6: Hermes skill and operating documentation

**Files:** `skills/threads-insights/SKILL.md`, `README.md`, `tests/test_skill_contract.py`.

**Skill contract:** prevent unequal-age lifetime comparisons, fake follower attribution, missing-to-zero conversion, and claims that inferred acceleration proves an official Meta distribution event.

- [x] Write failing static skill-contract test before creating `SKILL.md`.
- [x] Add evidence labels, same-age rule, missing-data rule, collector command, sampling guidance, and WhatsApp source convention.
- [x] Document migration/env/collector/scheduler setup.
- [x] Verify skill contract and full test suite.

## Verification

Fresh local verification command sequence:

```bash
python -m pip install -e . --no-deps --no-build-isolation
python -m compileall -q src scripts
python -m pytest -q
```

Expected test result for this implementation: `26 passed`.

Also verify the CLI exits with configuration error code 2 when required environment variables are absent and does not expose credential values.
