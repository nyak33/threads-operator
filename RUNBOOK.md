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
7. `migrations/007_publish_recovery.sql` for bounded automatic publishing recovery, retry metadata and the `retrying` / `needs_attention` queue states.

For an existing deployment, apply any not-yet-applied forward migrations. Do not drop production tables just to reach the latest schema.

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

Credentials alone never enable live posting. Internally, the public `threads_publish` capability is granted only after these live-post gates pass. Dry-run and ordinary diagnostic use never receive that capability.

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

New content may enter as `draft`. Draft ingress never promotes it automatically. Only rows for the selected `account_key` are eligible for publishing. `approved` rows become eligible only when `scheduled_at <= now`; an optional campaign code can further narrow the queue.

For near-scheduled posting, run the deterministic queue worker every minute. A post scheduled for 06:00 should therefore normally be picked up around 06:00–06:01. The worker never deliberately publishes before `scheduled_at`.

The worker conditionally claims `approved -> posting` before calling Threads. On success, the main Threads post ID is persisted before optional replies are attempted and completed work becomes `posted`.

### Automatic recovery

Retryable failures before a main post ID is confirmed do **not** become terminal immediately:

```text
approved -> posting -> retrying -> posting -> posted
```

The recovery window is bounded to 30 minutes from the original `scheduled_at`. Each retry is persisted with `attempt_count`, `next_retry_at`, `retry_deadline_at` and credential-safe structured Meta diagnostics in `last_error_meta`.

A queue-level retry starts a fresh publish transaction, so a failed main-post container is not blindly reused. Container-readiness errors may be retried briefly inside one transaction; broader transient failures return to the queue and use a fresh container on the next attempt.

Examples treated as retryable include transport/network errors, HTTP 429, Meta 5xx and generic/unknown publish HTTP 400 responses that are not clearly permanent. Unknown 400s are bounded by the same 30-minute window rather than being retried forever.

Clearly permanent failures such as invalid OAuth/token state, revoked permission, account/app restriction, unsupported operation or clearly invalid parameters move to `needs_attention` without consuming the full recovery window.

If the 30-minute deadline is reached, the row becomes `needs_attention` and is not published late. Expired retry rows remain claimable only so the worker can perform this terminal state transition; it does not call Threads after the deadline.

If the network fails after Threads accepted a publish but before the returned ID can be persisted, do not blindly reset the row to `approved`. Reconcile the Threads account and queue state first; external publication cannot be made perfectly transactional with Supabase.

Partial failures after the main post ID is already known remain conservative/manual so the main post is not duplicated while attempting to recover a reply chain.

## 11. Updating the Operator

Before pulling new code, preserve local account files and browser profiles outside the repository. Then:

```bash
git pull --ff-only
bash scripts/bootstrap.sh
```

Apply any new forward database migrations, including `006_security_hardening.sql` and `007_publish_recovery.sql` on deployments that predate them. Bootstrap is intended to be safe to run again. Re-run `doctor` for each enabled account after upgrades.

When upgrading an older deployment that uses campaign-specific legacy executors, inventory those executors before switching them to the shared publisher. Do not assume pulling this repository automatically changes external Hermes cron jobs or scripts outside the repository.

## 12. Repository Security

Keep `main` protected in GitHub and require the repository CI workflow before merge when repository settings permit it. This is a GitHub repository setting rather than runtime code.

The CI workflow uses read-only repository permission, immutable action SHAs, the Python test suite and a dependency vulnerability audit. Treat a failed security/dependency check as a release blocker until reviewed.

Do not commit production `.env` files, browser profiles, cookies, access tokens, Supabase service-role keys, or LLM provider keys.

Never publish diagnostic/control text to a production Threads account. Real integration publishing tests require a dedicated test account. Unit/regression tests must use mocks or stubs.

## 13. Failure Rules

- Missing account selector: stop; never guess an account.
- Missing account env: stop; do not fall back to another account's credentials.
- Invalid/non-official Threads API base URL: stop before sending the Threads token.
- Login/CAPTCHA/2FA challenge: stop Activity collection; do not bypass it.
- OAuth/permission/restriction error: stop automatic publish recovery and move the row to attention; do not retry as transient.
- Transport, rate-limit, Meta 5xx or unclassified non-permanent publish failure: enter bounded automatic recovery until success or the 30-minute deadline.
- Recovery deadline reached: do not publish late; move the row to `needs_attention` with diagnostics.
- Lost queue claim: do not publish.
- Partial publish: retain known IDs and reconcile; do not blindly requeue the whole row.
- Supabase unavailable: do not claim persistence or successful posting state.
- Hermes/model generation failure: do not create or approve a placeholder draft.
