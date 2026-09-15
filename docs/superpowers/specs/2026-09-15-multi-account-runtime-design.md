# Threads Operator Multi-Account Runtime — Design Specification

Date: 2026-09-15
Status: Approved in chat; implementation authorized

## 1. Objective

Turn `threads-operator` into a portable, account-aware runtime that can be cloned onto a fresh VPS/Hermes installation, configured with local secrets, and operated without rebuilding scripts by hand.

One repository installation must be able to operate multiple Threads accounts while keeping each account's API token, browser profile/session, database credentials, queue, and runtime settings isolated.

Hermes is the operator/executor. The repository owns deterministic Threads behavior. Secrets and account-specific runtime state stay outside GitHub.

## 2. Success Criteria

The implementation is successful when:

1. A fresh VPS can clone the repository, run one bootstrap command, add an account environment file, and run the operator.
2. One installation can configure two or more Threads accounts and select the intended account explicitly for every account-bound command.
3. Account A can use a different Threads token, browser profile, Supabase project, queue, and settings from Account B.
4. Existing official Threads Insights collection remains functional.
5. The existing read-only Activity/Follows collector remains functional and is isolated per account.
6. Posting can consume approved/due content from Supabase using an account-scoped queue, with duplicate-resistant claiming and safe failure states.
7. Auto-posting is disabled by default and cannot become enabled merely because credentials exist.
8. Real tokens, browser profiles, cookies, passwords, session files, Supabase service keys, and customer data never enter the repository.
9. GitHub CI passes the full regression suite before integration to `main`.

## 3. Runtime Model

```text
Fresh VPS / Hermes
       |
       v
clone threads-operator
       |
       v
bootstrap installation
       |
       +------------------------------+
       |                              |
       v                              v
~/.threads-operator/accounts/     repository code
  syaqir.env                        shared by all accounts
  brand_b.env
  clinic_c.env
       |
       v
explicit account selection
       |
       +------------+--------------+--------------+
       |            |              |              |
       v            v              v              v
   Insights      Activity       Publisher       Doctor
 Threads API   browser profile  Threads API    local checks
       |            |              |
       +------------+--------------+
                    |
                    v
          account-selected datastore
          (same or different Supabase)
```

There is one code installation, not one code clone per Threads account.

## 4. Account Selection and Isolation

### 4.1 Account home

Default runtime home:

`~/.threads-operator`

It may be overridden globally with `THREADS_OPERATOR_HOME`.

Account environment files live at:

`<home>/accounts/<account-key>.env`

Browser profiles may live anywhere, but the recommended path is:

`<home>/browser-profiles/<account-key>`

### 4.2 Account key

Account keys are operator-local stable identifiers such as:

- `syaqir`
- `brand_b`
- `clinic_c`

They are not Threads usernames and must contain only letters, digits, `_`, and `-`.

Every account-bound command must resolve the account from either:

1. explicit `--account <account-key>`, or
2. `THREADS_ACCOUNT=<account-key>`.

If neither is present, the command fails rather than guessing.

### 4.3 Secret precedence

Account-scoped credentials are loaded from the selected account file. Global process environment must not silently substitute another account's Threads token or Supabase service key.

Global process environment is reserved for operator-wide settings such as `THREADS_OPERATOR_HOME`, `THREADS_ACCOUNT`, and logging.

This prevents a cron job for one account from accidentally using credentials exported for another account.

## 5. Account Environment Contract

Required for official API/Insights:

- `THREADS_ACCESS_TOKEN`
- `THREADS_USER_ID`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Optional/common:

- `THREADS_API_BASE_URL=https://graph.threads.net/v1.0`
- `THREADS_ACCOUNT_SAMPLE_MINUTES=15`
- `THREADS_BROWSER_PROFILE=<path>`
- `ACTIVITY_FOLLOW_COLLECTOR_ENABLED=false`
- `THREADS_POSTING_ENABLED=false`
- `THREADS_EXECUTION_MODE=approval_required`
- `THREADS_QUEUE_TABLE=threads_publish_queue`
- `THREADS_QUEUE_CAMPAIGN_CODE=`

Each account file can point at the same Supabase project or a different Supabase project.

## 6. Commands

The installed CLI is `threads-operator`.

Required commands:

- `threads-operator accounts list`
- `threads-operator doctor --account <key>`
- `threads-operator insights --account <key>`
- `threads-operator activity-follow --account <key> [--dry-run]`
- `threads-operator publish --account <key> [--dry-run]`

All account-bound commands load exactly one selected account file.

Compatibility entrypoints may remain for the current Insights and Activity modules, but new deployment instructions use the unified CLI.

## 7. Insights

Insights continues to use the official Threads API and existing immutable snapshot model.

The selected account supplies the Threads user ID/token and Supabase connection. Snapshot records already contain Threads account/post identifiers, so the deterministic analytics model remains unchanged.

## 8. Activity / Browser Session

The existing Activity/Follows collector is included in this runtime.

Each account uses its own persistent Chromium profile via `THREADS_BROWSER_PROFILE`.

The operator does not store a Threads password in GitHub and does not implement CAPTCHA/2FA bypass. Initial login/session establishment is a deployment step. The collector reuses the authenticated persistent browser profile.

For matching notifications to owned posts, the selected account's official Threads API post list is preferred over an unscoped shared database list. This avoids cross-account post matching when multiple accounts share one Supabase project.

Activity observations must persist `account_key`, and deduplication must be account-scoped.

## 9. Publishing

### 9.1 Safety defaults

Publishing requires both:

- `THREADS_POSTING_ENABLED=true`
- `THREADS_EXECUTION_MODE=auto_post`

Default examples use:

- `THREADS_POSTING_ENABLED=false`
- `THREADS_EXECUTION_MODE=approval_required`

`--dry-run` may inspect the next eligible queue item without claiming or posting it.

### 9.2 Portable queue

The repository provides a generic Supabase table named `threads_publish_queue`.

Minimum fields:

- `id`
- `account_key`
- `campaign_code` (optional)
- `main_post_text`
- `reply_texts` JSON array
- `status`
- `scheduled_at`
- `claimed_at`
- `threads_main_post_id`
- `threads_reply_ids`
- `posted_at`
- `last_error`
- timestamps

Eligible work is `status='approved'` and due (`scheduled_at <= now()`).

Every query and claim is filtered by the selected `account_key`. Optional `THREADS_QUEUE_CAMPAIGN_CODE` adds a campaign filter.

### 9.3 Duplicate resistance

Publishing follows this state transition:

`approved -> posting -> posted`

A conditional Supabase PATCH claims one row only when it is still `approved`. Concurrent workers that lose the claim receive no row and do not publish.

After the main Threads post is published, its ID is persisted before replies are attempted. If a later reply fails, the row becomes `failed` with the known main post ID retained. The operator must never mark the row `posted` unless the requested publish sequence completed.

A network failure between a successful Threads publish and persistence of the returned ID cannot be made perfectly idempotent with the available external APIs; such cases must fail visibly for manual reconciliation rather than automatically retrying blindly.

## 10. Threads API Adapter

The API adapter keeps existing read-only methods and adds deterministic text publishing primitives:

1. create text container
2. publish container
3. create/publish a reply by passing the parent Threads post ID

Transient publication readiness/rate/server failures may be retried with bounded backoff. Authentication/permission errors fail immediately.

## 11. Bootstrap and Fresh VPS Deployment

`scripts/bootstrap.sh` must:

- require Python 3.11+
- create `.venv` if missing
- install this repository with its declared dependencies
- create the runtime account/browser directories
- never create or copy real credentials
- be safe to run again

`scripts/add_account.sh <account-key>` must:

- validate the account key
- refuse to overwrite an existing account file
- create a placeholder account env file with restrictive file permissions
- create the recommended browser-profile directory

The user/Hermes then supplies the real local values outside GitHub.

## 12. Doctor

`doctor` performs deterministic local validation without posting:

- selected account exists
- required API/database values are present
- sample interval is valid
- execution mode is valid
- browser profile is configured when Activity persistence is enabled
- posting configuration is internally consistent

It prints a machine-readable/concise result and must not print secret values.

## 13. Security

Mandatory:

- account `.env` files are ignored
- runtime home is outside the repository by default
- no credential values in logs or doctor output
- no browser cookies/profile data in Git
- no username/password login automation
- no CAPTCHA/2FA bypass
- browser Activity remains read-only
- auto-post remains opt-in per account
- Supabase service role is never exposed to browser/client-side code
- shared-database tables are scoped by `account_key` where account-local rows could otherwise collide

## 14. Repository Documentation

The repository root should expose:

- `GOAL.md`: what this operator is for
- `RUNBOOK.md`: how Hermes installs, adds accounts, selects an account, runs jobs, and handles failures
- `PROGRESS.md`: current implementation status / next work, without secrets
- `accounts/example.env`: safe account template

`README.md` provides the human-facing quick start and points to the runbook.

## 15. Non-Goals for This Change

Do not add:

- a web dashboard
- n8n dependency
- LLM/content-generation logic inside the deterministic runtime
- unrestricted automatic replies/likes/follows
- automated password login or challenge bypass
- account-specific business strategy
- complex central fleet management across multiple VPSs

## 16. Verification

Before integration:

- all previous Insights tests pass
- all Activity collector tests pass
- account loader tests prove explicit account isolation
- tests prove global account secrets cannot silently override the selected account file
- tests prove shared-database queue claims include `account_key`
- tests prove publishing is disabled by default
- tests prove a conditional claim prevents publishing unclaimed work
- tests cover Threads text container/publish request construction
- secret scan passes
- Python compile check passes
- GitHub Actions is green
