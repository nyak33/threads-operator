# Hermes Content Generation and Security Hardening Design

## Goal

Keep Threads Operator deterministic and provider-agnostic while allowing a Hermes installation to optionally generate content with its own configured LLM and submit that content into the existing account-scoped Supabase publish queue as a draft.

## Secret Boundary

Threads Operator must not own, read, require, or document any LLM provider API key.

Hermes/global runtime owns shared model/provider credentials. Per-account Threads Operator environment files continue to own account-scoped Threads credentials, Supabase credentials, browser profile paths, and posting safety settings.

Recommended hierarchy:

```text
Hermes runtime
  global/provider secrets
    LLM API key(s)

Threads Operator repository
  code + migrations + tests + safe examples
  no production secrets
  no LLM provider integration

~/.threads-operator/accounts/<account>.env
  THREADS_ACCESS_TOKEN
  THREADS_USER_ID
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  THREADS_BROWSER_PROFILE
  posting/activity/queue settings
```

If several personal accounts intentionally share one Supabase project, each account can point to the same project while mutable rows remain scoped by `account_key`. For client or high-isolation deployments, each account may point to a separate Supabase project.

## Optional Hermes Content Generation

The default workflow remains unchanged:

```text
ChatGPT / external content brain -> Supabase draft/approved queue -> Threads Operator -> Threads
```

Optional Hermes generation is an orchestration mode:

```text
Hermes + Hermes-owned LLM -> generated text -> Threads Operator draft ingress -> Supabase -> approval/schedule -> Threads Operator publisher
```

Threads Operator provides only a deterministic draft-ingress command. It does not call an LLM, select a model, hold provider credentials, write prompts, or auto-approve generated content.

The ingress command writes only `status=draft` rows for the explicitly selected account. Generated content therefore follows the same review, scheduling, claim, publish, partial-failure, and account-isolation rules as externally supplied content.

## Draft Ingress Contract

Add:

```text
threads-operator enqueue-draft --account <key> --text <main-post-text>
```

Optional arguments:

- `--reply` may be repeated to add reply-chain text.
- `--campaign-code` sets the queue campaign code.
- `--scheduled-at` accepts an ISO-8601 timestamp; when omitted, Supabase/default current time is used.

The command must:

- require explicit account resolution using the existing account loader;
- use the selected account's Supabase configuration;
- insert `account_key=<selected account>` and `status=draft`;
- never publish or mark content approved;
- return a non-secret JSON summary;
- never inspect or require an LLM API key.

## Security Hardening

### Threads API destination

`THREADS_API_BASE_URL` must be HTTPS and restricted to the official `graph.threads.net` host. This prevents a malicious or accidental account environment from redirecting a Threads access token to an arbitrary host.

### Supabase grants

A new forward-only migration must revoke `anon` and `authenticated` access from operator-owned Insights tables and grant only the minimum required access to `service_role`. Existing migrations are not rewritten as the sole upgrade mechanism because deployed databases may already have applied them.

### CI supply-chain posture

GitHub Actions workflow must:

- declare minimal read-only repository permissions;
- pin third-party GitHub Actions to immutable commit SHAs;
- run compile/tests;
- run a Python dependency vulnerability audit.

### Branch protection

Repository branch protection is an account/repository setting, not code. The runbook should recommend enabling required CI checks for `main`; code changes do not pretend to enforce this setting.

## Non-Goals

- No OpenAI/Qwen/Kimi/other provider SDK in this repository.
- No model selection logic in Threads Operator.
- No LLM API key in account examples or operator configuration.
- No automatic content approval.
- No automatic live posting as a consequence of generation.
- No migration of existing legacy campaign executors in this patch.

## Verification

The patch is complete when tests demonstrate:

- non-official Threads API base URLs are rejected before network use;
- draft ingress inserts only a selected-account `draft` row;
- CLI draft ingress does not invoke publishing;
- the security migration revokes public/authenticated grants;
- repository secret scans still pass;
- full compile/test CI and dependency audit pass.
