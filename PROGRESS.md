# PROGRESS — Threads Operator

## Current Milestone

Portable multi-account runtime with approval-gated engagement and account-scoped persona support.

## Done

- Historical Threads Insights collector and regression suite.
- Read-only Activity/Follows collector implementation and tests.
- Strict multi-account account-file configuration contract.
- Account-scoped Activity persistence.
- Threads text publishing and account-scoped queue contract.
- Fresh-VPS bootstrap and account initializer.
- Unified CLI for accounts, doctor, Insights, Activity, draft ingress, and publishing.
- Optional Hermes-generated-content boundary: Hermes owns model/provider credentials; Threads Operator accepts generated text only as `draft`.
- Account-scoped `enqueue-draft` path that never constructs the Threads publishing API.
- Explicit Hermes external-trend ingress that preserves provenance separately from manual URL ingestion.
- Official Threads API host validation before credentials can be sent.
- Forward Supabase privilege-hardening migration.
- GitHub Actions hardening with read-only permissions, immutable action SHAs, and dependency vulnerability auditing.
- Bootstrap build-tool hardening to avoid the audited vulnerable setuptools range.
- Deployment/runbook documentation for shared or separate Supabase layouts.
- Approval-gated external reply queue with Telebot approve/edit/reject flow and separate live engagement kill-switch.
- Account-scoped persona contract at `personas/<account-key>.md`; Hermes must load the exact account persona and fail closed if missing.
- `personas/syaqir.md` defining the current account's natural Malaysian rojak/bapak-bapak reply voice and anti-fabrication rules.
- Feature-branch verification: compile succeeded, 136 tests passed with 1 skipped, and dependency audit reported no known vulnerabilities.

## Plug-and-Play Hardening (branch `feat/plug-and-play-hardening`, 2026-09-23)

- Repository reconciliation audited and recorded in `docs/repo-reconciliation.md` (branches classified; VPS-only runtime artifacts categorized A–F).
- Category A runtime code ported into `scripts/`: insights collector wrapper, activity collector wrapper, stale-claim watchdog, trend-engagement watchdog, own-replies watchdog, trend approval-timeout watchdog, publish alerts, rollup rebuild.
- Supabase schema reconciliation: 9 production `threads_*` tables had no creating migration; `migrations/013_missing_base_tables_reconcile.sql` recreates the 6 operator-relevant ones from verified evidence (apply BEFORE 010–012 on a fresh project). `migrations/002_post_daily_rollups.sql` consolidated into a single file and **applied to production** (externally verified 2026-09-23: table exists, RLS on, expected indexes present).
- Machine dependencies removed: `/home/admin` literals eliminated from `src/`/`scripts/`; hardcoded Telegram chat-id fallback removed from publish alerts (alerts no-op when unconfigured); repo-root override renamed `THREADS_OPERATOR_REPO`.
- `scripts/doctor.sh`: single PASS/WARN/FAIL diagnostic (Python, package, account config, live Supabase connectivity + 12-table schema probe, Telegram, Hermes env/cron, runtime dirs). Verified live (11 pass / 1 warn / 0 fail) and in clean room (correct FAIL modes).
- `scripts/install_jobs.sh` + `config/runtime-jobs.json` + `config/trend-discovery-prompt.md.tmpl`: declarative, idempotent runtime-job install/converge/remove for the 7 Hermes cron jobs; dry-run and status modes; verified against the live store (7/7 present).
- Clean-room verification (`docs/fresh-install-verification.md`): fresh clone bootstraps, CLI works, 557 tests pass, no `/home/admin` in code paths. Two real defects found and fixed: missing exec bits on bootstrap/add_account/publish-queue-worker, and a doctor.sh false-PASS on Supabase probe errors.
- External fresh-Supabase replay (2026-09-23): all 14 migrations applied + idempotency re-run passed; `002` confirmed applied to production. It surfaced a fresh-install RLS/privilege gap — fixed by `migrations/014_fresh_schema_security_reconcile.sql` (enable RLS + revoke anon/authenticated + grant service_role + idempotent policy on the 7 operator tables), regression-guarded by `tests/test_migration_014_security_reconcile.py`. **014 re-replay verified same day:** full chain from zero (`013→014`) on the throwaway project passed with the Security Advisor ERROR cleared and 014 idempotency confirmed; `014` also applied to production (RLS on all 7, zero anon/authenticated grants, service_role policies present, recorded in migration history).
- Test suite: 565 passed, 1 skipped (baseline 542 + 15 contract tests + 8 migration-014 security regression tests; no test weakened).

## Superpower Task #2 — Context-Aware Replies + Lead/DM Handoff (2026-09-25)

### Task 2A — Context Engine (DONE)

- `src/threads_operator/reply_context.py`: deterministic context assembly for own-post replies.
- All 11 required context fields: account, root post, parent reply, current reply, conversation chain (bounded to depth 3), topic/topic_tag, content pillar, post objective, CTA, prior interactions (bounded to 10), account persona.
- Fail-closed on: missing account context, root post unresolvable, cross-account data mixing, uncertain event identity.
- Never fabricates missing context; falls back to reply_row data when threads_posts unavailable.
- `own_replies.classify_reply()`: wrapper that assembles context + classifies intent + persists to DB.

### Task 2B — Intent + Lead Detection (DONE)

- `src/threads_operator/reply_intent.py`: deterministic intent classifier with 8 categories:
  `casual`, `question`, `positive_engagement`, `objection`, `buying_intent`, `cta_match`, `potential_lead`, `needs_human_review`.
- Contextual evidence required — keyword/rule matching is evidence, not auto-classification.
- Ambiguous cases fail conservatively or require Hermes/human interpretation.
- `should_create_dm_opportunity()`: fail-closed gate requiring confidence >= 0.5 AND intent in (cta_match, buying_intent, potential_lead).

### DM Opportunity State Machine (DONE)

- `src/threads_operator/dm_opportunity.py`: full lifecycle `detected → drafted → awaiting_approval → approved → sending → sent` with terminal states `rejected/failed/expired/cancelled`.
- `migrations/015_reply_context_intent.sql`: adds context/intent fields to `threads_own_reply_engagement` and creates `threads_dm_opportunities` table with RLS, unique dedupe constraint on (account_key, from_username, root_post_id), and CAS-guarded transitions.
- Dedup enforced at DB level + application level. Same reply processed twice returns `already_exists=True`.

### Tests (33 new, all passing)

- `tests/test_reply_context_intent.py`: 33 tests covering all 10 required test scenarios + integration tests.
- Full regression suite: **660 passed, 1 skipped** (baseline 627 + 33 new).

### Task 2C — Telegram DM Approval Workflow (DONE, 2026-09-25)

- DM opportunity → Hermes-generated draft → persisted exactly → Telegram approval card → Approve/Edit/Reject → `approved` (or terminal rejection/expiry). **No Threads DM is sent** — Task 2D consumes only `approved` rows.
- `dm_opportunity.py`: `draft_dm_opportunity`, `mark_dm_awaiting_approval`, `approve_dm_opportunity` (snapshots `dm_approved_text`), `reject_dm_opportunity`, `edit_dm_opportunity_draft` (stays `awaiting_approval`, never auto-approves), `record_dm_approval_card_sent`, `list_dm_opportunities_needing_card`, `expire_stale_opportunities`. All CAS-guarded + account-scoped; expiry fail-closed.
- `own_replies.generate_dm_draft`: persona + root post + CTA + incoming reply + intent + lead score. No fabricated context; Hermes owns wording; no provider credentials in the operator.
- `operator_cli`: `own-replies dm {draft,card,approve,edit,reject,mark-card-sent,needing-card}`.
- `workflow_telegram.DMDispatcher` (`dmopp:` prefix) reuses `_BaseWorkflowDispatcher` + `PendingEditStore` (30-min edit expiry, restart-safe); post-edit refreshed card; `DMTelegramBridge` gateway alias.
- Gateway adapter (`plugins/platforms/telegram/adapter.py`): `dmopp:` route + `_get_dm_approval_bridge` + edit-session interceptor. Reuses `_dispatch_threads_operator_callback`; no separate dispatch system.
- `migrations/016_dm_approval_workflow.sql`: `drafted_at`, `edited_at`, `approval_message_ref`, `approved_by`, `reject_reason` + watchdog index.
- Watchdog: `run_dm_approval_pass()` in `own_replies_watchdog_5m.py` — draft → card → stamp `approval_message_ref`; idempotent (no duplicate cards); reuses the existing 5-min job, no new runtime job.
- `tests/test_dm_approval.py`: 35 tests covering all 16 required scenarios. Full suite **698 passed, 1 skipped**.

## Deployment Next

1. ~~Apply `migrations/002_post_daily_rollups.sql` to production Supabase~~ — DONE (externally verified 2026-09-23; table exists, RLS on, PK/indexes present; `rebuild_rollups.py` blocker cleared).
2. ~~Apply `migrations/014_fresh_schema_security_reconcile.sql` to production Supabase + re-replay the full chain incl. 014 on the throwaway project~~ — DONE (externally verified 2026-09-23: production 014 applied and recorded in migration history, all 7 tables RLS-enabled with service_role policies and zero anon/authenticated grants, `threads_own_reply_engagement` error cleared; throwaway re-replay of `013→014` passed, 014 idempotent, Security Advisor ERROR cleared).
3. ~~Reconcile/retire old remote branches per `docs/repo-reconciliation.md` classifications~~ — DONE (2026-09-24: obsolete fix/feat branches deleted local + remote; remaining refs are `main` + 3 intentional `backup/*`; one worktree, no stashes).
4. ~~Fresh-VPS end-to-end run including a throwaway Supabase project replay of all migrations incl. 014~~ — DONE (see `docs/fresh-install-verification.md`; only live-posting/browser steps remain deliberately out of scope).
5. Multi-account hardening soak (per-account failure isolation under simultaneous operation).
6. **Apply `migrations/015_reply_context_intent.sql` to production Supabase** when ready to enable context/intent layer.
7. ~~Telegram DM approval workflow (Task #2 continuation)~~ — DONE (Task 2C, 2026-09-25); apply `migrations/016_dm_approval_workflow.sql` to production to enable.
8. **Safe browser-based Threads DM sender** (Task 2D) — consumes approved DM opportunities only.

## Deliberately Later / Non-Goals

- Web dashboard.
- Central multi-VPS fleet management.
- Automatic repair of major Threads UI/authentication changes.
- LLM provider/content-generation logic inside the deterministic Threads Operator itself.
- Provider-specific LLM API keys in Threads Operator account ENV files.
- Automatic approval merely because content was generated by Hermes.
- Automated likes/follows/unrestricted replies.
- **Live Threads DM sending** — deferred to Task #3 (safe browser-based sender).
