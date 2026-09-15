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

## 3. Account Environment

Use `accounts/example.env` as the contract. Each account file supplies its own:

- Threads access token and Threads user ID;
- Supabase URL and service-role key;
- optional browser profile;
- Activity enable switch;
- posting safety switches;
- queue/campaign settings.

Accounts may point to the same Supabase project or different Supabase projects. Shared databases still isolate mutable operator rows by `account_key`.

## 4. Database Migrations

For a fresh database, apply the repository migrations in filename order that are relevant to the enabled capabilities. At minimum for the current full operator:

1. `migrations/001_threads_insights.sql`
2. `migrations/001b_threads_insights_snapshots.sql`
3. `migrations/003_activity_follow_events.sql` if Activity is enabled
4. `migrations/004_threads_publish_queue.sql` if queue publishing is enabled

A deployment that already applied the older single-account Activity migration needs the multi-account upgrade migration documented in this repository before the new Activity writer is enabled. Preserve historical data; do not drop and recreate the Activity table just to upgrade it.

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

## 8. Multiple Accounts

Every scheduled job must name the account explicitly. For example, one cron may run Insights for `syaqir` and another for `brand_b`; both use the same installation but load different account files.

Do not rely on a shell-exported Threads token to distinguish accounts. The selected account file is the source of account-scoped credentials.

A failure in one account job should be reported for that account and must not cause another account's credentials or browser profile to be used as fallback.

## 9. Publish Queue Rules

The generic queue is `threads_publish_queue` unless an account overrides `THREADS_QUEUE_TABLE`.

Only rows for the selected `account_key` are eligible. By default, only `status=approved` rows whose `scheduled_at` is due can be claimed. An optional campaign code further narrows the queue.

The worker conditionally claims `approved -> posting` before calling Threads. The main Threads post ID is persisted before optional replies are attempted. Completed work becomes `posted`; visible partial failures become `failed` and retain known post IDs for reconciliation.

If the network fails after Threads accepted a publish but before the returned ID can be persisted, do not blindly reset the row to `approved`. Reconcile the Threads account and queue state first; external publication cannot be made perfectly transactional with Supabase.

## 10. Updating the Operator

Before pulling new code, preserve local account files and browser profiles outside the repository. Then:

```bash
git pull --ff-only
bash scripts/bootstrap.sh
```

Bootstrap is intended to be safe to run again. Re-run `doctor` for each enabled account after upgrades.

## 11. Failure Rules

- Missing account selector: stop; never guess an account.
- Missing account env: stop; do not fall back to another account's credentials.
- Login/CAPTCHA/2FA challenge: stop Activity collection; do not bypass it.
- OAuth/permission error: stop and repair credentials; do not retry as a transient publish failure.
- Lost queue claim: do not publish.
- Partial publish: retain known IDs and reconcile; do not blindly requeue.
- Supabase unavailable: do not claim persistence or successful posting state.
