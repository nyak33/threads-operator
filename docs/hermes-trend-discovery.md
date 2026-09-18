# Hermes External Trend Discovery

## Objective

Hermes discovers useful public Threads posts from other accounts. Threads Operator validates and stores the permalink under the selected account, then optionally enriches factual engagement evidence. Content adaptation remains outside the deterministic runtime.

## Safe boundary

- Hermes may search/browse public Threads content and decide which URLs are worth recording.
- Hermes must not write directly to `threads_trend_candidates`.
- Use the account-scoped Threads Operator CLI so URL validation, deduplication and provenance are deterministic.
- Use `--external` only for posts Hermes discovered externally. User-supplied URLs stay on the normal manual ingress path.
- Browser enrichment is read-only and must stop on login, CAPTCHA, 2FA or security challenges.

## One discovery iteration

For every candidate URL:

```bash
RESULT="$(.venv/bin/threads-operator trend add \
  --account syaqir \
  --url "$THREADS_URL" \
  --external)"
printf '%s\n' "$RESULT"
```

If the JSON result says `status=inserted` and returns an `id`, enrich that candidate:

```bash
.venv/bin/threads-operator trend enrich \
  --account syaqir \
  --id "$CANDIDATE_ID"
```

If the result says `status=existing`, do not alert again and do not create another row.

## Discovery policy

Keep the first deployment simple:

1. Search for recent public Threads posts relevant to the target account's content themes and Malaysian audience.
2. Prefer posts with visibly strong engagement or unusually fast engagement for their age.
3. Store the permalink first; never fabricate post IDs or metrics.
4. Enrich only newly inserted candidates.
5. Alert the user only when at least one new external candidate was inserted.
6. In the alert, send the permalink plus observed text/engagement facts. Do not auto-generate or auto-publish derivative content.

The candidate table is the handoff point to ChatGPT for later analysis and adaptation.

## Suggested cadence

Run every 3 hours during active hours. A failed search/discovery pass should not change posting state and should not affect the publish worker.
