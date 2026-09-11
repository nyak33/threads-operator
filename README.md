# Threads Operator

Reusable Hermes skills and deterministic helpers for operating a Threads account. The first implemented subsystem is `threads-insights`, which preserves historical account/post snapshots so growth and velocity can be calculated later instead of relying only on live lifetime totals.

## Historical Insights MVP

### What it stores

- account snapshots: followers, profile/account views and supported engagement totals
- post snapshots: views, likes, replies, reposts, quotes, shares
- post age at capture time
- raw `NULL` for unavailable metrics rather than fabricated zeroes

Raw snapshots are append-only. `threads_daily_rollups` is intentionally rebuildable.

### Setup

1. Apply `migrations/001_threads_insights.sql` to the target Postgres/Supabase project.
2. Copy `.env.example` to `.env` and fill values locally. Never commit `.env`.
3. Install the package:

```bash
python -m pip install -e .
```

4. Export/load the environment variables, then run one collection:

```bash
python scripts/collect_insights.py
```

The command prints a compact JSON summary and never prints credentials.

### Recommended scheduler

Run the collector every 5 minutes. The collector decides whether each post is due:

| Post age | Minimum interval |
|---|---:|
| 0–2h | 5m |
| 2–6h | 15m |
| 6–24h | 30m |
| 1–3d | 60m |
| 3–7d | 6h |
| >7d | 24h |

Account snapshots default to every 15 minutes.

Example cron entry after installing into the same Python environment:

```cron
*/5 * * * * cd /path/to/threads-operator && /path/to/python scripts/collect_insights.py >> /var/log/threads-insights.log 2>&1
```

Use deployment-specific secret injection rather than putting credentials in the cron line.

## Analytics primitives

`threads_operator.insights` currently provides deterministic helpers for:

- follower/metric growth percentage
- views or engagements per minute
- age-aware snapshot intervals
- conservative second-wave pattern detection

Reports should distinguish `measured`, `calculated`, `inferred`, and `attributed` statements. Exact follower or profile-view attribution to a specific post is not claimed without an explicit attribution mechanism.

## WhatsApp convention

For simple Threads source attribution, deployment profiles may use the prefilled message:

`Hi, saya datang dari Threads.`

The public repository intentionally contains no real phone number.

## Safety

- no posting or replying is performed by the Insights collector
- no credentials are stored in repository files
- raw metric history is not overwritten by the collector
- missing metrics stay unknown/null
