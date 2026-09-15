# GOAL — Threads Operator

## Objective

Provide one portable deterministic Threads runtime that Hermes can clone onto a fresh VPS and operate for one or many Threads accounts without rebuilding account-specific scripts.

## End State

A deployment should be able to:

1. clone this repository;
2. run the bootstrap script;
3. add one local account environment file per Threads account;
4. establish a persistent browser login only when Activity collection is needed;
5. run `threads-operator doctor --account <key>`;
6. run Insights, Activity collection, deterministic draft ingress, and approved-queue publishing for that selected account.

## Boundaries

- GitHub stores code, migrations, safe examples, tests, and operating instructions.
- Real Threads API tokens, Supabase service keys, browser profiles, cookies, and account sessions stay local to the deployment.
- Hermes operates the system; deterministic code owns Threads API calls, datastore state transitions, account selection, and safety gates.
- Content strategy and LLM generation stay outside the deterministic Threads Operator runtime.
- Hermes may optionally generate content using model/provider credentials owned by Hermes, then pass only the generated text into the operator's account-scoped draft ingress.
- Threads Operator does not own, read, select, or require an LLM provider/API key.
- Generated content enters as `draft`; generation alone never approves or publishes it.
- Live posting is opt-in per account and disabled by default.

## Success Criteria

- One installation can operate multiple isolated Threads accounts.
- Every account-bound command explicitly selects one account.
- Account A cannot silently inherit Account B's exported credentials.
- Activity remains read-only and account-isolated.
- External or Hermes-generated text can be inserted as an account-scoped draft without invoking Threads publishing.
- Queue publishing claims approved work before posting and preserves partial-failure state.
- Threads tokens are sent only to the official HTTPS Threads API host.
- A fresh VPS can be brought to a runnable state from this repository plus local account credentials; optional LLM credentials remain entirely in Hermes.
