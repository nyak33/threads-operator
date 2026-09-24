# Threads Operator

Deterministic runtime for operating one or more Threads accounts from a single VPS + Hermes installation. It owns account selection, official Threads API publishing, approval-gated engagement, trend-candidate intake, historical Insights collection, and read-only Activity/Follows collection — with Supabase as the source of truth and Telegram as the operator interface. Real credentials and browser sessions never enter Git.

## What It Does

The operating model is: **content enters a Supabase queue → a scheduler/executor publishes approved, due rows via the official Threads API → inbound replies and trend candidates are drafted for a human → Telegram approve/edit/reject → executed replies and engagement metrics flow back into Supabase.**

```text
 content (external / Hermes-generated draft)
        |  status=draft -> approved (human or upstream flow)
        v
 +-------------------+      official Threads API      +-----------+
 | Supabase queues   |  ---------------------------->  |  Threads  |
 | (source of truth) | <----------------------------   |   API     |
 +-------------------+   post IDs, insights, replies   +-----------+
        ^    |                                              |
        |    v                                              |
 | publish-worker / watchdogs (Hermes cron)                 |
        |    |                                              |
        |    +-- trend discovery (Hermes agent, read-only) -+
        |    +-- own-replies watchdog (draft -> Telegram approval)
        |    +-- trend-engagement watchdog (draft -> Telegram approval)
        v
 Telegram operator: approve / edit / reject / timeout
```

Only flows that exist today are shown. LLM content generation itself is **not** part of this repo: Threads Operator receives already-generated text (or ingests trend permalinks) and handles everything deterministic around it.

## Current Capabilities

All items below are verified against code and the running VPS (see `docs/fresh-install-verification.md` and the current test suite/CI).

### Content Management
- Account-scoped publish queue (`threads_publish_queue`, default; overridable per account).
- Draft ingress via `enqueue-draft` — never auto-promotes; human/upstream approval moves rows to `approved`.
- Per-row **topic** → published as Meta `topic_tag` on the root post (1 topic/post, normalized to Meta rules); survives retries/requeues.
- Main post + optional follow-up replies in one queue row; the main post ID is persisted before replies are attempted.
- Platform content guardrails: standard root/reply text is limited to 500 characters; supported draft ingress validates the whole chain before storing it, and the Threads API boundary validates every individual publish call. Long-text attachments are a separate unsupported path; see `docs/threads-platform-rules.md`.

### Publishing
- Scheduled publishing: only `status=approved` rows due by `scheduled_at` are claimed, with a conditional claim (lost claim never publishes).
- `publish-worker` cron variant: automatic requeue of transient failures (network, 429, 5xx, non-OAuth 400) back to `approved` with persisted backoff; OAuth/permission errors stay visibly `failed`.
- Half-published thread recovery via `heartbeat_at` — auto-reclaim/resume without human intervention.
- Live posting is opt-in per account: requires `THREADS_POSTING_ENABLED=true` **and** `THREADS_EXECUTION_MODE=auto_post`.

### Engagement
- **Workflow A — trend-engagement**: trend candidate → original own post, approval-gated, enqueued through the normal publish queue.
- **Workflow B — own-replies**: discovers replies under the account's own posts via the Graph API, drafts persona-voiced responses, publishes only after Telegram approval.
- Both are fully approval-gated (`pending_approval -> approved -> executing -> posted`, plus `rejected`); editing keeps a reply pending; a separate kill-switch `THREADS_ENGAGEMENT_ENABLED=true` gates live execution.
- Watchdogs draft replies, send Telegram approval cards, publish execute-approved replies, and time out stale approvals.

### Trend Discovery
- Hermes-side discovery (agent cron job) searches public Threads, ingests **only** via `trend add --external` (records `candidate_role=external_trend`, `discovery_method=hermes_external`, `discovered_by=hermes`), dedupes by permalink, and alerts only on new inserts.
- Read-only factual enrichment (`trend enrich`) captures observed post facts through the account's browser profile; provenance is verifiable via `trend show`.

### Analytics / Insights
- Append-only historical account + post snapshots (views, likes, replies, reposts, quotes, shares), post-age-aware sampling, raw `NULL` preserved for unavailable metrics (never fabricated zeroes), daily rollups.

### Multi-Account
- Account config, personas, browser profiles, and queue scoping are per-account and isolated; one shared codebase/install.
- Verified today: strict account-file contract (`~/.threads-operator/accounts/<key>.env`), account-scoped Activity persistence, per-account persona fail-closed resolution, account-scoped publish queue and engagement/trend tables.
- Production currently runs one account (`syaqir`). Multi-account hardening (per-account failure isolation guarantees, simultaneous-operation soak) is roadmap, not current.

### Telegram / Operator Interface
- Approval cards for drafted replies (own-replies + trend-engagement), approve/edit/reject/execute flows, publish alerts, trend-discovery alerts, and watchdog failure alerts.
- Alert chat resolves from `TELEGRAM_HOME_CHANNEL` / `TELEGRAM_CHAT_ID` in the Hermes env file; when unconfigured, alerts no-op instead of leaking to a hardcoded chat.

### Automation
- 7 Hermes cron jobs declared in `config/runtime-jobs.json` (6 script jobs + 1 agent trend-discovery job): insights collector, activity-follow collector, stale-claim watchdog, trend-engagement watchdog, own-replies watchdog, trend approval-timeout watchdog, trend discovery. Install/verify/remove via `scripts/install_jobs.sh`.

## Known Limitations

**Platform limitations** (Threads/Meta prevents these):
- Replies to arbitrary third-party posts are not generally available via the API; engagement is limited to own-post replies and approved original posts. (`GET /{web_id}` for external roots returns error subcode 33; keyword search is own-posts-only on this app's permissions.)
- `Followed from your post` per-post attribution is not exposed as an official Insights metric — the read-only Activity collector approximates it with explicit confidence levels instead.
- Rate limits, token expiry/permissions, and API instability are Meta-side; the operator handles them with retries/backoff but cannot eliminate them.

**Current implementation limitations** (not yet built):
- Runtime-job installation is idempotent but the *initial* job creation for a brand-new account still runs `scripts/install_jobs.sh` manually (no auto-registration on `add_account.sh`).
- Browser-session bootstrap for Activity collection is a manual login step per account (no automated credential entry — by design; the operator never automates passwords/CAPTCHA/2FA).
- The full Supabase migration chain has been replayed against a brand-new empty project (externally verified 2026-09-23 — all apply + idempotency re-run pass). It surfaced a fresh-install RLS gap, fixed by `migrations/014_fresh_schema_security_reconcile.sql` (apply LAST on a fresh project to reproduce production's RLS/service_role-only posture; production already has this posture so 014 is posture-preserving there).

## How It Works

Components and responsibilities:

- **Supabase** — source of truth: publish queue, trend candidates, engagement queue, insights snapshots/rollups, activity events, gateway keys. All state transitions are SQL-conditional.
- **Threads API (official Graph)** — publishing, own-post reply discovery, insights metrics.
- **`threads_operator` Python package** (`src/`) — account config resolution, API adapters, publisher/publish-worker, engagement/own-replies/trend workflows, insights + activity collectors, Telegram alert/callback senders. Deterministic; no LLM calls.
- **Hermes** — the scheduler and the only LLM brain: cron jobs run repo scripts (watchdogs/collectors) or the trend-discovery agent prompt; Hermes' configured model generates reply text inside those flows.
- **Telegram** — operator interface for approvals and alerts.
- **Runtime jobs** — declared in `config/runtime-jobs.json`, installed into Hermes cron by `scripts/install_jobs.sh`.
- **Browser profile** (optional, per account) — persistent Chromium profile for read-only Activity/trend enrichment.

## Requirements

**Runtime:**
- Linux VPS, Python 3.11+, bash.
- Hermes (scheduler + Telegram gateway + the model used inside engagement/trend flows).
- Supabase project (PostgREST reachable; service-role key).
- Threads/Meta app credentials (access token + user ID) per account.
- Telegram bot token + chat id (in the Hermes env file).
- Optional: Chromium (for Activity collection / trend enrichment browser profile).

**Development / optional (NOT runtime requirements):**
- pytest, pip-audit (CI/dev only).
- Graphify, Diagram Design, Superpowers — documentation/planning tools used during this repo's hardening; the operator does not need them to run.

## Installation

**Current: semi-automated.** Bootstrap, account scaffolding, doctor, tests, and runtime-job install are scripted and clean-room verified. Supabase project creation, credential entry, browser login, and one SQL migration are manual. **Target: plug-and-play** — not claimed until a fresh-VPS end-to-end run (including a throwaway Supabase project) passes.

```bash
git clone https://github.com/nyak33/threads-operator.git
cd threads-operator
./scripts/bootstrap.sh                 # venv + package + operator home
./scripts/add_account.sh <account-key> # creates ~/.threads-operator/accounts/<key>.env (600)
# edit the account file with real credentials
# apply Supabase migrations (see below)
./scripts/doctor.sh --account <account-key>
./scripts/install_jobs.sh --account <account-key>
```

**Supabase:** create a project, then apply migrations in the SQL editor. Order on a fresh project: `013_missing_base_tables_reconcile.sql` FIRST (it recreates the base tables the early migrations assume), then `001`, `001b`, `002`, `003`–`012` in filename order, then `014_fresh_schema_security_reconcile.sql` LAST (reconciles fresh-install RLS/privilege posture with production: RLS on, anon/authenticated locked out, service_role-only). `006_security_hardening.sql` tightens privileges. Never apply migrations destructively to production; 002/013/014 are additive.

## Configuration

| Category | Where | Required | Keys (values never in git) |
|---|---|---|---|
| Account | `~/.threads-operator/accounts/<key>.env` (600) | REQUIRED | `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` |
| Account behavior | same file | OPTIONAL (defaults exist) | `THREADS_POSTING_ENABLED=false`, `THREADS_EXECUTION_MODE=approval_required`, `THREADS_QUEUE_TABLE=threads_publish_queue`, `THREADS_ACCOUNT_SAMPLE_MINUTES=15`, `ACTIVITY_FOLLOW_COLLECTOR_ENABLED=false`, `THREADS_BROWSER_PROFILE`, `THREADS_ENGAGEMENT_ENABLED`, `THREADS_QUEUE_CAMPAIGN_CODE` |
| Telegram | `~/.hermes/.env` | REQUIRED for alerts | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL` (or `TELEGRAM_CHAT_ID`) |
| Persona | `personas/<key>.md` (in repo, no secrets) | REQUIRED for voiced generation | public style rules only |
| Schedules | `config/runtime-jobs.json` | GENERATED into Hermes cron | per-job schedule/deliver |

Classification: **REQUIRED** values must be real (not `replace_me`) before doctor passes; **OPTIONAL** values have safe defaults; **GENERATED** artifacts are produced by scripts, not hand-edited. Template: `accounts/example.env`.

## Operations

- **Validate an account:** `./scripts/doctor.sh --account <key>` (PASS/WARN/FAIL; exit 1 on any FAIL)
- **Tests:** `.venv/bin/python -m pytest -q`
- **Runtime jobs:** `./scripts/install_jobs.sh --status` / `--account <key>` / `--remove --account <key>`
- **Add an account:** `./scripts/add_account.sh <key>` → fill credentials → doctor
- **Update code:** `git pull`, re-run `./scripts/bootstrap.sh`, re-run tests; runtime jobs converge via `install_jobs.sh`
- **Recovery/rollback, browser sessions, failure handling, migration details:** [`RUNBOOK.md`](RUNBOOK.md)

## Current Features

Everything under "Current Capabilities" above — verified in code and on the running VPS. Nothing is listed here on the strength of a branch or a TODO.

## Roadmap

### Near Term — Plug-and-Play Hardening (in progress on `feat/plug-and-play-hardening`)
- Complete repo reconciliation of all VPS-only runtime code (done: watchdogs, collectors, rollups, alerts).
- Reproducible migrations: replay the full set against a throwaway Supabase project.
- Bootstrap/doctor/runtime-job installer (done: `doctor.sh`, `install_jobs.sh`, `runtime-jobs.json`).
- Clean-room install testing (done for code path; Supabase replay pending).
- Multi-account hardening (failure-isolation soak, simultaneous-operation guarantees).
- Auto-register runtime jobs on `add_account.sh`.

### Next — Operator Reliability
- Stronger queue/retry recovery, health monitoring, better failure alerts, automatic recovery, observability.

### Later — Intelligence / Optimization
- Content performance feedback loop, insights-driven content generation, richer trend selection, account-level optimization, automated experimentation.

### Future / Optional
- Selective-repost executor (API adapter already exposes the repost endpoint), additional platform support.

## Repository Docs

- [`RUNBOOK.md`](RUNBOOK.md) — operations, deployment, migrations, browser sessions, failure handling.
- [`GOAL.md`](GOAL.md) — system boundaries and intended end state.
- [`PROGRESS.md`](PROGRESS.md) — implementation status.
- [`docs/repo-reconciliation.md`](docs/repo-reconciliation.md) — Part 1–7 audit: branches, VPS-vs-repo artifacts, Supabase schema reconciliation.
- [`docs/fresh-install-verification.md`](docs/fresh-install-verification.md) — Part 22 clean-room results.
- [`docs/superpowers-pilot.md`](docs/superpowers-pilot.md) — Superpowers pilot task record.
- [`docs/threads-platform-rules.md`](docs/threads-platform-rules.md) — current Threads character limits, chain-writing rules, official sources, and re-verification policy.
- [`docs/two-engagement-workflows.md`](docs/two-engagement-workflows.md), [`docs/hermes-engagement-approval.md`](docs/hermes-engagement-approval.md), [`docs/hermes-trend-discovery.md`](docs/hermes-trend-discovery.md), [`docs/hermes-content-generation.md`](docs/hermes-content-generation.md), [`docs/publish-queue-worker.md`](docs/publish-queue-worker.md), [`docs/activity-follow-collector.md`](docs/activity-follow-collector.md) — subsystem designs.

## Safety

- No real credentials, browser profiles, or cookies in git; no LLM provider credentials in this repo or account env files.
- Account selection is explicit; a lost queue claim never publishes; OAuth/permission failures are not treated as transient.
- Activity collection is read-only; the operator never automates passwords, CAPTCHA, or 2FA.
- Generated content enters as `draft`; live posting and live engagement each have separate opt-in kill-switches; account-voiced generation is persona fail-closed.

## Development

```bash
python -m pip install -e ".[dev]"
python -m compileall -q src scripts
python -m pytest -q
python -m pip_audit --skip-editable
```

GitHub Actions runs the same compile/test verification and dependency audit for pull requests.
