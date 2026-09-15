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
6. run Insights, Activity collection, and approved-queue publishing for that selected account.

## Boundaries

- GitHub stores code, migrations, safe examples, tests, and operating instructions.
- Real API tokens, Supabase service keys, browser profiles, cookies, and account sessions stay local to the deployment.
- Hermes operates the system; deterministic code owns API calls, datastore state transitions, and safety gates.
- Content strategy and LLM generation are not required inside this runtime.
- Live posting is opt-in per account and disabled by default.

## Success Criteria

- One installation can operate multiple isolated Threads accounts.
- Every account-bound command explicitly selects one account.
- Account A cannot silently inherit Account B's exported credentials.
- Activity remains read-only and account-isolated.
- Queue publishing claims work before posting and preserves partial-failure state.
- A fresh VPS can be brought to a runnable state from this repository plus local credentials.
