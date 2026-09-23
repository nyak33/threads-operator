# RUNBOOK — Threads Operator

This is the deterministic operating guide for Hermes or a human operator. Do not paste real credentials into GitHub, prompts, logs, issues, or pull requests.

## 1. Fresh VPS Install

Clone the repository and bootstrap it:

```bash
git clone https://github.com/nyak33/threads-operator.git
cd threads-operator
bash scripts/bootstrap.sh
```

The bootstrap creates `.venv` and the default local runtime home at `~/.threads-operator` with `accounts/` and `browser-profiles/` directories.

If a different local runtime home is required, export `THREADS_OPERATOR_HOME` before running bootstrap and all operator commands.

## 2. Add an Account

Create one isolated local account configuration:

```bash
bash scripts/add_account.sh syaqir
```

This creates:

- `~/.threads-operator/accounts/syaqir.env`
- `~/.threads-operator/browser-profiles/syaqir/`

The account file is local runtime state. It must not be copied into this repository.

If a prepared `.env` is supplied by the account owner instead, place it at the same account path and set its permission to owner-only. Do not merge several accounts into one environment file.

Add additional accounts by repeating the command with a different account key. One repository installation can operate multiple Threads accounts.

## 3. Secret and Account Environment Boundary

Use `accounts/example.env` as the Threads Operator account contract. Each account file supplies its own:

- Threads access token and Threads user ID;
- Supabase URL and service-role key;
- optional browser profile;
- Activity enable switch;
- posting safety switches;
- queue/campaign settings.

LLM/model provider API keys do **not** belong in these account files or anywhere in the Threads Operator repository.

If Hermes is configured to generate content, Hermes owns the provider/model credentials in its own global/runtime configuration. Threads Operator receives only the already-generated text.

Recommended layout:

```text
Hermes global/runtime config
  -> LLM provider/model credentials

~/.threads-operator/accounts/account_a.env
  -> account_a Threads + Supabase + browser/posting settings

~/.threads-operator/accounts/account_b.env
  -> account_b Threads + Supabase + browser/posting settings
```

Accounts may point to the same Supabase project or different Supabase projects. Shared databases still isolate mutable operator rows by `account_key`. For client/high-isolation deployments, prefer a separate Supabase project/service-role key per account or client.

## 4. Database Migrations

For a fresh database, apply the repository migrations in filename order that are relevant to the enabled capabilities. For the current full operator:

1. `migrations/001_threads_insights.sql`
2. `migrations/001b_threads_insights_snapshots.sql`
3. `migrations/003_activity_follow_events.sql` if Activity is enabled
4. `migrations/004_threads_publish_queue.sql` if queue publishing or draft ingress is enabled
5. `migrations/005_activity_multi_account_upgrade.sql` may also be applied; it is designed to be harmless when the fresh account-scoped Activity schema already exists.
6. `migrations/006_security_hardening.sql` to remove client-role access from operator-owned Insights tables and retain service-role access.

For an existing deployment, apply any not-yet-applied forward migrations. Do not drop production tables just to reach the latest schema.

The two engagement workflows (trend→own-post drafting and own-post reply handling) require `migrations/010_two_engagement_workflows.sql`. It widens the `threads_trend_candidates` status CHECK (adding `drafted`, `pending_approval`, `queued`, `skipped`, `posted`, `failed`) and creates `threads_own_reply_engagement`. Until it is applied, `trend-engagement draft/approve` and all `own-replies` writes fail against the live schema (the code fails closed — no partial writes). See [`docs/two-engagement-workflows.md`](docs/two-engagement-workflows.md) for the full architecture, Telegram cards, and watchdog schedules.

### Existing single-account Activity database

If the older single-account `003_activity_follow_events.sql` was already applied, run `005_activity_multi_account_upgrade.sql` before enabling the new Activity writer. It preserves the table instead of dropping historical data.

Because an old row contains no operator-local account key, the upgrade temporarily labels historical rows as `legacy`. Before enabling collection, map those historical rows to the correct new local account key if you know which account they belong to. Do not leave `legacy` mixed with new data if you expect one continuous historical series.

## 5. Browser Session for Activity

Activity/Follows uses a persistent authenticated Chromium profile. Use a different profile directory for every Threads account.

Establish the login session interactively in that profile. The operator does not automate passwords and does not bypass login checkpoints, CAPTCHA, 2FA, or other security challenges.

The Activity collector itself remains read-only: it reads the Activity/Follows document and does not click, like, reply, follow, message, or modify the account.

## 6. Validate an Account

Run:

```bash
.venv/bin/threads-operator doctor --account syaqir
```

The doctor checks local configuration without posting and never prints credential values.

List configured account keys with:

```bash
.venv/bin/threads-operator accounts list
```

## 7. Run One Account

Insights:

```bash
.venv/bin/threads-operator insights --account syaqir
```

Activity dry-run:

```bash
.venv/bin/threads-operator activity-follow --account syaqir --dry-run
```

Activity persistence requires `ACTIVITY_FOLLOW_COLLECTOR_ENABLED=true` in that selected account file.

Publishing dry-run:

```bash
.venv/bin/threads-operator publish --account syaqir --dry-run
```

Live publishing requires both of these values in that selected account file:

```text
THREADS_POSTING_ENABLED=true
THREADS_EXECUTION_MODE=auto_post
```

Credentials alone never enable live posting.

## 8. Optional Hermes Content Generation

The default workflow may remain unchanged: ChatGPT or another external content system writes content into Supabase and Hermes/Threads Operator executes it.

Optionally, Hermes may use the LLM/model already configured in Hermes to generate content itself. Keep generation separate from Threads Operator:

```text
Hermes model -> generated text -> enqueue-draft -> Supabase draft -> review/approval -> publisher
```

Submit generated main-post text as a draft:

```bash
.venv/bin/threads-operator enqueue-draft \
  --account syaqir \
  --text "$GENERATED_TEXT" \
  --campaign-code HERMES_GENERATED
```

Optional replies can be supplied with repeated `--reply` arguments. `--scheduled-at` may be supplied as an ISO-8601 timestamp.

`enqueue-draft` is deliberately safe: it uses the selected account's Supabase configuration, writes `status=draft`, does not call any LLM, does not approve the content and does not publish to Threads.

Do not add an LLM API key to `accounts/<account>.env` just to use this feature. The model credential stays with Hermes.

See `docs/hermes-content-generation.md` for the complete optional setup.

## 9. Multiple Accounts

Every scheduled job must name the account explicitly. For example, one cron may run Insights for `syaqir` and another for `brand_b`; both use the same installation but load different account files.

Do not rely on a shell-exported Threads token to distinguish accounts. The selected account file is the source of account-scoped credentials.

A failure in one account job should be reported for that account and must not cause another account's credentials or browser profile to be used as fallback.

If Hermes generates content for several accounts, generation jobs must also pass the intended account explicitly to `enqueue-draft`. Do not infer the target account from the generated text.

## 10. Publish Queue Rules

The generic queue is `threads_publish_queue` unless an account overrides `THREADS_QUEUE_TABLE`.

New content may enter as `draft`. Draft ingress never promotes it automatically. Only rows for the selected `account_key` are eligible for publishing, and by default only `status=approved` rows whose `scheduled_at` is due can be claimed. An optional campaign code further narrows the queue.

The worker conditionally claims `approved -> posting` before calling Threads. The main Threads post ID is persisted before optional replies are attempted. Completed work becomes `posted`; visible partial failures become `failed` and retain known post IDs for reconciliation.

For cron-driven publishing use the `publish-worker` command: identical publishing plus automatic requeue of transient failures (network, 429, 5xx, non-OAuth 400) back to `approved` for the next tick, while OAuth/permission errors stay `failed`. Install and verify it per [`docs/publish-queue-worker.md`](docs/publish-queue-worker.md):

```bash
hermes cron create "*/5 * * * *" --name "Publish Queue Worker (syaqir)" \
  --script "$PWD/scripts/publish_queue_worker_hermes.py" \
  --no-agent
```

(Edit `ACCOUNT`/`CAMPAIGN_CODE` in the wrapper; one cron job per account.)

If the network fails after Threads accepted a publish but before the returned ID can be persisted, do not blindly reset the row to `approved`. Reconcile the Threads account and queue state first; external publication cannot be made perfectly transactional with Supabase.

## 11. Updating the Operator

Before pulling new code, preserve local account files and browser profiles outside the repository. Then:

```bash
git pull --ff-only
bash scripts/bootstrap.sh
```

Apply any new forward database migrations, including `006_security_hardening.sql` on deployments that predate it. Bootstrap is intended to be safe to run again. Re-run `doctor` for each enabled account after upgrades.

## 12. Repository Security

Keep `main` protected in GitHub and require the repository CI workflow before merge when repository settings permit it. This is a GitHub repository setting rather than runtime code.

The CI workflow uses read-only repository permission, immutable action SHAs, the Python test suite and a dependency vulnerability audit. Treat a failed security/dependency check as a release blocker until reviewed.

Do not commit production `.env` files, browser profiles, cookies, access tokens, Supabase service-role keys, or LLM provider keys.

## 13. Failure Rules

- Missing account selector: stop; never guess an account.
- Missing account env: stop; do not fall back to another account's credentials.
- Invalid/non-official Threads API base URL: stop before sending the Threads token.
- Login/CAPTCHA/2FA challenge: stop Activity collection; do not bypass it.
- OAuth/permission error: stop and repair credentials; do not retry as a transient publish failure.
- Lost queue claim: do not publish.
- Partial publish: retain known IDs and reconcile; do not blindly requeue.
- Supabase unavailable: do not claim persistence or successful posting state.
- Hermes/model generation failure: do not create or approve a placeholder draft.
