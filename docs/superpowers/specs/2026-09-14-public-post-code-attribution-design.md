# Public Post Code Attribution Design

## Objective

Add a shared, account-scoped public post-code system used by every Threads post executor so future `Followed from your post` Activity notifications can be attributed to an exact source post without relying on repeated text.

The public marker is deliberately human-readable and placed at the start of the post:

```text
Day 583

<original post text>
```

`Day` is presentation only. The numeric value is an internal attribution marker.

## Core decisions

1. One shared allocator is used by all executors. Individual campaign executors must not invent their own counters.
2. Codes are unique within a Threads account, not globally across all accounts.
3. Codes use only 1–4 decimal digits and never use leading zeroes.
4. Public code length grows with the internal sequence bucket:
   - internal sequence 1–9 → 1-digit public code
   - 10–99 → 2-digit public code
   - 100–999 → 3-digit public code
   - 1000–9999 → 4-digit public code
5. Public numbers are a deterministic reversible permutation of the internal sequence within the same digit bucket. They therefore look jumbled instead of steadily increasing.
6. Maximum v1 capacity is 9,999 posts per account. V1 must hard-stop before allocating sequence 10,000. It must never recycle an old code.
7. The transformation is reversible keyed obfuscation, not a security boundary. Secrets stay outside Git.
8. Exact attribution uses two independent checks:
   - identity: Activity `Day N` resolves to a unique published post mapping for the same account
   - time: the mapped post was published at or before the follow-notification timestamp
9. Existing text matching remains as fallback for old posts without a code.
10. Exact code attribution is a separate confidence class: `exact` is stronger than current `high` text matching.

## Why not encode the timestamp into four digits

A 1–4 digit decimal code has only 9,999 possible non-zero values. A full date/time domain contains far more states, so a reversible timestamp encoding into four digits is impossible without collisions or lost information.

Instead, the code identifies the post record. The record stores the actual Threads post ID and publication timestamp. Looking up the code therefore answers both questions the operator needs: which post and when it was published.

## Public-code permutation

### Required properties

For each account and digit bucket, the transformation must be:

- deterministic
- one-to-one
- reversible
- restricted to the same digit length
- keyed so public codes do not reveal the simple internal sequence
- dependency-light

### V1 algorithm

Use a keyed affine permutation over each bucket.

For a bucket:

```text
1 digit:  start=1,    size=9
2 digits: start=10,   size=90
3 digits: start=100,  size=900
4 digits: start=1000, size=9000
```

Convert internal sequence `s` to zero-based `x = s - start`.

Derive account/bucket-specific `a` and `b` from HMAC-SHA256 using `THREADS_POST_CODE_SECRET`, `account_key`, scheme version, and bucket width. Choose `a` deterministically such that `gcd(a, size) = 1`; derive `b` modulo `size`.

Encode:

```text
y = (a*x + b) mod size
public_code = start + y
```

Decode:

```text
x = a_inverse * (y - b) mod size
internal_sequence = start + x
```

The implementation must test every value in all four buckets for round-trip correctness and uniqueness. This is obfuscation, not cryptographic format-preserving encryption.

## Database model

### `threads_post_code_counters`

One row per account.

Recommended fields:

- `account_key text primary key`
- `current_sequence integer not null default 0`
- `updated_at timestamptz not null default now()`

Allocation must be atomic in Postgres. Concurrent executors for the same account must receive different internal sequence values.

### `threads_post_codes`

Durable reservation/mapping table.

Recommended fields:

- `id bigint identity primary key`
- `account_key text not null`
- `internal_sequence integer not null check between 1 and 9999`
- `public_code integer`
- `scheme_version integer not null default 1`
- `status text not null` with `reserved`, `published`, `failed`
- `campaign_code text null`
- `executor_name text null`
- `source_ref text null`
- `thread_id text null`
- `reserved_at timestamptz not null default now()`
- `published_at timestamptz null`
- `failed_at timestamptz null`
- `failure_reason text null`
- `updated_at timestamptz not null default now()`

Required uniqueness:

- `(account_key, internal_sequence)`
- `(account_key, public_code)` when `public_code` is not null
- `thread_id` when not null

Do not require a foreign key to `threads_posts`; the post-code mapping is created at publish time and must remain valid even if the inventory sync is temporarily stale.

## Allocation lifecycle

### Reserve before publish

Every executor follows the same sequence:

```text
reserve atomic sequence
→ derive public code
→ persist public code on reservation
→ render post text with `Day N`
→ publish to Threads
→ finalize reservation with thread_id + published_at
```

If code persistence fails before publication, stop and do not publish.

### Publish failure

If Threads publication fails after reservation:

- mark reservation `failed`
- never recycle the internal sequence or public code

Gaps are expected and safer than reuse.

### Publish succeeds but finalization fails

This is the dangerous partial-failure case. The public post already contains its code, so the system must retain the reservation and later reconcile it.

A reconciliation command must be able to inspect own posts and recover the mapping by the unique `Day N` prefix. It must never create a second reservation for the same code.

## Shared library interface

Add one focused module in `threads_operator`, with an API equivalent to:

```python
reservation = allocator.reserve(
    account_key=...,
    campaign_code=...,
    executor_name=...,
    source_ref=...,
)

rendered_text = render_with_public_code(original_text, reservation.public_code)

# existing publisher posts rendered_text

allocator.mark_published(
    reservation_id=reservation.id,
    thread_id=threads_media_id,
    published_at=published_timestamp,
)
```

Executors must not know how counters or permutations work. They only consume the shared reservation API.

## Content rendering rules

Canonical format:

```text
Day <code>

<original text without leading blank lines>
```

Rules:

- marker must be at the beginning so Activity snippets are likely to preserve it
- no leading zeroes
- do not prepend twice if the content is retried
- preserve the original body exactly after the marker/blank-line boundary
- a retry of the same claimed queue item must reuse its existing reservation, not allocate another code

That last rule is essential. Executor idempotency must extend to code allocation.

## Executor integration

Every production Threads publisher must use the same allocator, including current campaign executors and future executors. Hermes must inventory the live deployment before editing because campaign scripts may live outside this repository.

The integration should occur immediately before the existing Threads publish call, after the queue row has been safely claimed but before the request is sent to Threads.

For queue-backed campaigns, persist the reservation reference on the queue item if practical. If changing campaign tables would create excessive schema churn, use a deterministic `source_ref` such as `<table>:<row-id>` and make the allocator return the existing reservation for that source reference.

The required invariant is:

> one logical posting attempt / source item → one reserved public code, even across retries or process restarts.

## Activity exact matching

When an Activity source snippet starts with a marker matching:

```regex
^\s*Day\s+([1-9]\d{0,3})\b
```

perform code matching before text matching.

A result is `exact` only when all of the following hold:

1. collector account is known
2. `(account_key, public_code)` resolves to exactly one mapping
3. mapping status is `published`
4. mapping has a `thread_id`
5. `published_at` is known
6. `published_at <= notification_datetime`

Optional consistency guard when `threads_posts` is available: its text should also begin with the same `Day N` marker.

If any exact-code validation fails, do not fabricate certainty. Fall back to the current text matcher.

## Attribution state and rematching

`threads_activity_events` remains append-only raw evidence. Do not update old raw rows merely because a better match becomes available later.

Add a rebuildable/current attribution layer keyed by Activity notification, for example `threads_activity_attributions`, with fields such as:

- `account_key`
- `notification_id`
- `matched_post_id`
- `matched_permalink`
- `public_code`
- `match_method`
- `match_confidence` (`exact`, `high`, `medium`, `low`, `unknown`)
- `source_event_id`
- `evaluated_at`

The rematcher can then improve an existing notification from `unknown` to `exact` or `high` without mutating raw Activity observations.

The summary view should use the latest raw notification state for `follow_count` and the current attribution row for source-post identity/confidence.

## Matching hierarchy

For future reports use this order:

1. `exact` — validated public code mapping + time check
2. `high` — unique eligible normalized text match
3. `medium` — repeated text, nearest-prior inference
4. `low` — multiple plausible candidates
5. `unknown` — no reliable candidate

Never promote fuzzy similarity alone to `exact`.

## Multi-account scope

The allocator is global across executors, but counters and public-code uniqueness are per Threads account.

Therefore:

```text
Account A → Day 583
Account B → Day 583
```

is valid because the actual lookup key is `(account_key, public_code)`.

The implementation must not assume one global account. Account selection comes from deployment/profile configuration.

## Secret handling

Use an environment variable such as:

```text
THREADS_POST_CODE_SECRET=<deployment secret>
```

Rules:

- never commit the secret
- never print it
- never include it in exception text
- derive account/bucket parameters from it
- record `scheme_version` in mappings
- do not rotate the secret casually; historical decoding depends on the correct scheme/key version

Because the database stores direct mappings, attribution does not require reverse decoding during normal operation. Decoding is primarily an audit/debug capability.

## Capacity and rollover

V1 supports exactly 9,999 allocated sequences per account. At sequence 9,999 the allocator must continue to work; the next reservation request must fail closed with a clear capacity error before anything is posted.

Do not recycle codes and do not silently introduce five digits.

Rollover/epoch design is deliberately deferred until capacity is materially close. It requires an explicit v2 design because exact attribution must never become ambiguous.

## Failure isolation

- allocator failure must affect only the post being prepared, not Insights or Activity collection
- Activity collector must continue to support legacy text matching
- code-generation failures must not corrupt queue status
- no new code path may publish a post after failing to persist its reservation/code
- reconciliation must be idempotent

## Verification requirements

Before enabling on production executors, prove:

1. encode/decode round trip for all 1–9,999 sequences
2. no duplicate public code within each account
3. code stays in the same digit bucket
4. concurrent reservation calls allocate unique sequences
5. failed posts do not recycle codes
6. retries reuse the same reservation for the same source item
7. successful post finalization stores exact thread ID and timestamp
8. reconciliation repairs a simulated post-success/finalize-failure case
9. Activity `Day N` match becomes `exact` only when time validation passes
10. future-dated mapped posts are rejected
11. legacy posts without a code still use high/medium/low/unknown text matching
12. full existing Insights and Activity test suites still pass
13. no secrets, cookies or session data appear in Git

## Deployment order

1. Implement and test allocator + migration in the repository.
2. Apply migration.
3. Dry-run allocator without publishing.
4. Integrate one low-risk executor first and publish a controlled test post.
5. Confirm mapping and Activity exact attribution.
6. Integrate remaining executors one by one.
7. Keep legacy matcher enabled permanently for old posts.
8. Only after all publishers are integrated should exact-code coverage be treated as expected for new content.

## Non-goals for v1

- encoding date/time directly into the public code
- five-digit codes
- code recycling
- hiding the existence of the marker from sophisticated observers
- replacing Threads post IDs as the canonical external identity
- changing existing campaign content-generation logic beyond prepending the marker
