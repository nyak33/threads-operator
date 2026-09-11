---
name: threads-insights
description: Use when the user asks about Threads performance, historical growth, follower changes, post velocity, same-age benchmarks, metric anomalies, content performance patterns, or whether Insights collection is current.
---

# Threads Insights

## Core rule

Use stored historical snapshots as the source of truth. Current lifetime totals alone cannot reconstruct an earlier growth curve.

## Evidence labels

Label important conclusions by evidence quality:

- `measured` — returned directly by the configured data source.
- `calculated` — deterministic arithmetic from measured snapshots.
- `inferred` — interpretation of a pattern such as fast-burn, slowdown, or second-wave behavior.
- `attributed` — conversion attribution backed by an explicit tracking mechanism.

Never present `inferred` as `measured`.

## Analysis contract

1. Compare posts using **same-age** checkpoints where possible: 15m, 30m, 1h, 3h, 24h, 3d, 7d.
2. Calculate growth and velocity only when both observations exist and elapsed time is valid.
3. A missing metric is unknown. Never convert missing data to zero.
4. Separate reach, engagement, authority, and business outcomes. Lower reach does not automatically mean worse content.
5. Report temporal correlation accurately. Example: `+8 followers during the three hours following publication` is acceptable; `this post generated 8 followers` is not unless attribution exists.
6. A detected second wave means measured velocity slowed and later materially accelerated. It **does not prove** Meta officially initiated a distribution wave.
7. Flag stale collection or partial API failures before drawing conclusions from incomplete history.

## Collection

Run the deterministic collector rather than asking the LLM to manufacture history:

```bash
python scripts/collect_insights.py
```

Recommended scheduler cadence is every 5 minutes. The collector itself applies age-based post sampling and a default 15-minute account snapshot interval, so older posts are not polled every run.

Raw snapshot tables are append-only:

- `threads_account_snapshots`
- `threads_post_snapshots`

`threads_daily_rollups` is derived and rebuildable.

## WhatsApp source convention

When a configured profile uses direct WhatsApp attribution for Threads, the standard prefilled message is:

`Hi, saya datang dari Threads.`

Treat this as source-level attribution to Threads, not exact post-level attribution.
