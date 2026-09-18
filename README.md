# Threads Operator

Portable deterministic helpers for operating one or many Threads accounts from a single VPS/Hermes installation.

The repository owns account selection, official Threads API calls, historical Insights collection, read-only Activity/Follows collection, account-scoped Supabase state and approved-queue publishing. Real credentials and browser sessions stay outside GitHub.

## Architecture

```text
one VPS / one Hermes / one threads-operator install
                       |
          +------------+------------+
          |                         |
      account A                   account B
  API token + user ID         API token + user ID
  browser profile            browser profile
  Supabase config            Supabase config
          |                         |
          +---- shared codebase ----+
```

Each account is selected explicitly. The operator never guesses which account should run and does not silently inherit another account's exported Threads/Supabase secrets.

## Fresh VPS Quick Start

```bash
git clone https://github.com/nyak33/threads-operator.git
cd threads-operator
bash scripts/bootstrap.sh
bash scripts/add_account.sh syaqir
```

Fill the local account file created at `~/.threads-operator/accounts/syaqir.env`, then validate it:

```bash
.venv/bin/threads-operator doctor --account syaqir
```

Add more accounts by repeating `add_account.sh` with another account key. One installation is shared; credentials, browser profiles and datastore settings remain separate.

See [`RUNBOOK.md`](RUNBOOK.md) for deployment, migration, browser-session, scheduling and failure-handling instructions. See [`GOAL.md`](GOAL.md) for system boundaries and [`PROGRESS.md`](PROGRESS.md) for current implementation status.

## Commands

```text
threads-operator accounts list
threads-operator doctor --account <key>
threads-operator insights --account <key>
threads-operator activity-follow --account <key> --dry-run
threads-operator enqueue-draft --account <key> --text <text>
threads-operator publish --account <key> --dry-run
threads-operator publish-worker --account <key> [--campaign-code <code>] [--dry-run]
threads-operator trend add --account <key> --url <threads-url> [--external] [--dry-run]
threads-operator trend list --account <key> [--status <status>]
threads-operator trend show --account <key> --id <candidate-id>
threads-operator trend enrich --account <key> --id <candidate-id>
threads-operator engagement propose-reply --account <key> --source-post-id <id> --url <url> --text <reply>
threads-operator engagement list --account <key> --status pending_approval
threads-operator engagement approve --account <key> --id <engagement-id>
threads-operator engagement edit --account <key> --id <engagement-id> --text <replacement>
threads-operator engagement reject --account <key> --id <engagement-id>
threads-operator engagement execute --account <key> --id <engagement-id> [--dry-run]
```

`publish` publishes one approved due row (single-shot). `publish-worker` is the cron-friendly variant: identical publishing, but transient failures are automatically requeued to `approved` for the next tick. See [`docs/publish-queue-worker.md`](docs/publish-queue-worker.md).

`--account` may be replaced by the operator-wide `THREADS_ACCOUNT` selector, but every account-bound command must resolve exactly one account.

## Account Configuration

Use [`accounts/example.env`](accounts/example.env) as the safe template. New deployments store real account files outside the repository at:

```text
~/.threads-operator/accounts/<account-key>.env
```

Each account can use the same Supabase project or a different one. Account-local mutable rows such as Activity observations and publish-queue work are scoped by `account_key` when a database is shared.

LLM provider credentials do **not** belong in Threads Operator account files. If Hermes is configured to generate content, its model/API credentials stay in Hermes' own global/runtime configuration. Threads Operator receives only the already-generated text. See [`docs/hermes-content-generation.md`](docs/hermes-content-generation.md).

## Account Personas

Public voice/style is account-scoped in versioned Markdown files:

```text
personas/<account-key>.md
```

For example, account `syaqir` uses [`personas/syaqir.md`](personas/syaqir.md). Another Threads account should have its own file, such as `personas/brand_a.md`.

Before Hermes generates a reply or other account-voiced text, it must load the persona matching the selected `--account`. Persona resolution is fail-closed: if `personas/<account-key>.md` is missing, Hermes must stop generation and report the missing persona instead of borrowing another account's voice.

Persona files are model-independent. They contain public style/content rules only; Hermes uses whatever provider/model is currently configured as its primary/default model. Do not put provider names, API keys, Threads credentials, Supabase credentials or unnecessary private information in persona files.

See [`personas/README.md`](personas/README.md) for the persona contract.

## Historical Insights

The Insights subsystem preserves historical account/post snapshots so growth and velocity can be calculated later rather than relying only on live lifetime totals.

It stores:

- account snapshots including supported follower/view/engagement totals;
- post snapshots including views, likes, replies, reposts, quotes and shares;
- post age at capture time;
- raw `NULL` for unavailable metrics rather than fabricated zeroes.

Raw snapshots are append-only. Partial metric responses remain valid. Historical all-`NULL` rows do not make a post appear fresh.

Recommended post sampling remains age-aware:

| Post age | Minimum interval |
|---|---:|
| 0–2h | 5m |
| 2–6h | 15m |
| 6–24h | 30m |
| 1–3d | 60m |
| 3–7d | 6h |
| >7d | 24h |

Account snapshots default to every 15 minutes.

## Activity / Follows Attribution

The optional Activity collector reads the authenticated Threads Activity/Follows page because `Followed from your post` information is not exposed as the same per-post official Insights metric.

Each account uses its own persistent Chromium profile. The collector is deliberately read-only: it does not click, type, like, reply, follow, message, replay private requests or bypass authentication challenges.

The Activity UI exposes source text/snippets rather than a guaranteed source-post ID. Matching therefore retains explicit high/medium/low/unknown confidence instead of presenting inferred attribution as exact fact.

The detailed design and limitations remain in [`docs/activity-follow-collector.md`](docs/activity-follow-collector.md).

## External Trend Discovery

Trend discovery remains a Hermes responsibility; Threads Operator owns deterministic validation, account scoping, storage and factual enrichment.

Manual URLs use the default ingress:

```bash
.venv/bin/threads-operator trend add --account syaqir --url "$THREADS_URL"
```

When Hermes itself discovers an external post, it must use the explicit external flag:

```bash
.venv/bin/threads-operator trend add \
  --account syaqir \
  --url "$THREADS_URL" \
  --external
```

That records `candidate_role=external_trend`, `discovery_method=hermes_external` and `discovered_by=hermes` without confusing automated discovery with a manual URL. If the insert returns a new candidate id, Hermes may run `trend enrich --id <id>` to capture observed post facts through the existing read-only browser profile. Existing permalinks remain deduplicated.

See [`docs/hermes-trend-discovery.md`](docs/hermes-trend-discovery.md) for the scheduled discovery flow.


## Approval-Gated Engagement

Hermes may generate short micro-replies for relevant external posts, but it cannot approve its own reply. Before generation, Hermes must load the selected account's exact `personas/<account-key>.md` file. Proposed replies are inserted into `threads_engagement_queue` as `pending_approval` and surfaced to Telebot.

The allowed reply state flow is:

```text
pending_approval -> approved -> executing -> posted
                 -> rejected
```

Editing a reply keeps it in `pending_approval`. Live execution is separately disabled by default and requires:

```text
THREADS_ENGAGEMENT_ENABLED=true
```

Even with that flag enabled, `engagement execute` conditionally claims only an already-`approved` row. The approval transition records the Telebot channel/reference for auditability. Duplicate actions are prevented per account + source permalink + action.

Reply publishing uses the official Threads reply mechanism through `reply_to_id`. The API adapter also exposes the official repost endpoint for the later selective-repost executor. Browser-driven likes remain outside this approval queue module.

See [`docs/hermes-engagement-approval.md`](docs/hermes-engagement-approval.md) for the Telebot workflow and safety boundary.

## Optional Hermes-Generated Drafts

The default workflow remains external/ChatGPT-created content entering Supabase before Threads Operator executes it.

Optionally, Hermes can use its own configured LLM/provider to generate content and submit the result as an account-scoped draft:

```bash
.venv/bin/threads-operator enqueue-draft \
  --account syaqir \
  --text "$GENERATED_TEXT" \
  --campaign-code HERMES_GENERATED
```

This command never calls an LLM, never approves content and never publishes. It inserts `status=draft` into the selected account's configured queue. Model choice and LLM API keys remain entirely outside Threads Operator.

## Approved-Queue Publishing

The generic Supabase queue is `threads_publish_queue`. Eligible work is account-scoped, `approved`, and due by `scheduled_at`. A worker conditionally claims the row before any Threads publish call.

Live publishing is disabled by default. It requires both:

```text
THREADS_POSTING_ENABLED=true
THREADS_EXECUTION_MODE=auto_post
```

A dry run may inspect eligible work without claiming or publishing it.

The publisher persists the main Threads post ID before attempting optional replies. If a later step fails, the queue retains known IDs and moves to a visible failed state for reconciliation instead of blindly retrying the whole chain.

## Database Migrations

For a fresh full deployment, apply the relevant migrations in filename order:

- `001_threads_insights.sql`
- `001b_threads_insights_snapshots.sql`
- `003_activity_follow_events.sql` for Activity
- `004_threads_publish_queue.sql` for publishing
- `005_activity_multi_account_upgrade.sql` where relevant for upgraded Activity data
- `006_security_hardening.sql` to tighten operator table privileges
- `007_threads_engagement_queue.sql` for approval-gated external engagement actions

Existing deployments that already applied the original single-account Activity migration must use the multi-account upgrade migration described in the runbook rather than dropping historical data.

## Safety

- no real credentials, browser profiles or cookies belong in Git;
- no LLM provider credentials belong in Threads Operator or its account ENV files;
- account selection is explicit;
- Threads API credentials are restricted to the official HTTPS API host;
- Activity collection is read-only;
- missing Insights metrics remain unknown/null;
- generated content enters the queue as `draft` only;
- live posting is opt-in per account;
- live engagement has a separate opt-in kill-switch;
- Hermes-generated replies require an explicit human approval transition before execution;
- account-voiced generation must use the exact matching persona file and never silently fall back to another account's persona;
- a lost queue claim never publishes;
- OAuth/permission failures are not treated as transient readiness failures;
- the operator does not automate passwords, CAPTCHA or 2FA bypass;
- no LLM provider or content-generation logic is required by the deterministic runtime.

## Development

```bash
python -m pip install -e ".[dev]"
python -m compileall -q src scripts
python -m pytest -q
python -m pip_audit --skip-editable
```

GitHub Actions runs the same compile/test verification and dependency audit for pull requests.
