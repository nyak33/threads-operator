# PROGRESS — Threads Operator

## Current Milestone

Portable multi-account runtime: one GitHub repository / one installation / multiple isolated Threads accounts.

## Done

- Historical Threads Insights collector and regression suite.
- Read-only Activity/Follows collector implementation and tests carried forward from its feature branch.
- Multi-account runtime design and implementation plan.
- Strict account-file configuration contract.
- Account-scoped Activity persistence design.
- Threads text publishing and account-scoped queue contract.
- Fresh-VPS bootstrap and account-initializer contract.
- Unified CLI contract for accounts, doctor, Insights, Activity, and publishing.

## In Progress

- Final implementation verification through GitHub Actions.
- Review of all multi-account, publishing-safety, migration, and deployment changes.

## Next

1. Require full CI green.
2. Review final branch diff for regressions and credential leakage.
3. Merge the verified feature to `main`.
4. On a real VPS, supply local account credentials, apply database migrations, establish browser profiles where Activity is required, run `doctor`, then perform read-only/dry-run smoke tests before enabling live posting.

## Deliberately Later

- Web dashboard.
- Central multi-VPS fleet management.
- Automatic repair of major Threads UI/authentication changes.
- LLM/content-generation logic inside the deterministic operator.
- Automated likes/follows/unrestricted replies.
