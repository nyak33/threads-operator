# Two Engagement Workflows

Approval-gated engagement for Threads accounts, built on the existing
operator infrastructure (account configs, personas, Supabase stores, Telebot
approval architecture, publish queue, Graph API adapter). No model or
provider is hardcoded — draft generation resolves the operator's configured
LLM chain at call time (`content_generation.py`).

## Workflow A — Trend candidate → ORIGINAL own post

Trend candidates (from the existing trend-discovery collectors) are scored
against the account's themes, drafted as ORIGINAL posts in the account
persona (never copies of the source), sent to Telegram for approval, and —
only after approval — inserted into the normal publish queue. The existing
publish-worker owns actual publication.

State machine on `threads_trend_candidates` (migration 010 widens the
status CHECK):

```text
discovered -> drafted -> pending_approval -> approved -> queued -> posted
                                           -> rejected (terminal)
                                           -> skipped   (terminal)
                                           -> failed
```

Commands:

```bash
# score + draft eligible candidates (discovered/drafted -> pending_approval)
.venv/bin/threads-operator trend-engagement draft --account syaqir [--limit 5] [--threshold 0.6] [--dry-run]

# list candidates in a workflow state (includes draft text)
.venv/bin/threads-operator trend-engagement list --account syaqir [--status pending_approval]

# approve -> enqueue through the EXISTING publish queue (idempotent;
# double-approval returns the existing queue row, topic/topic_tag preserved)
.venv/bin/threads-operator trend-engagement approve --account syaqir --id <candidate-id>

# edit the draft while pending (500-char limit enforced)
.venv/bin/threads-operator trend-engagement edit --account syaqir --id <candidate-id> --text <new draft>

# reject / skip — terminal, never proposed again, nothing enqueued
.venv/bin/threads-operator trend-engagement reject --account syaqir --id <candidate-id>
.venv/bin/threads-operator trend-engagement skip   --account syaqir --id <candidate-id>
```

Telegram card (callback data `trendeng:<action>:<id>`, handled by
`TrendEngagementDispatcher` in `workflow_telegram.py`):

```text
🆕 TREND CANDIDATE
Source: @username
URL: ...
Reason: ...
Suggested angle: ...
Draft (n chars): ...
Topic: ...
[Approve] [Edit] [Reject] [Skip]
```

Watchdog: `~/.hermes/scripts/trend_engagement_watchdog_30m.py`, every 30
minutes (cron job "Threads Trend Engagement Watchdog (syaqir)").

## Workflow B — Replies under our OWN posts

Periodic Graph-API discovery of new replies under the account's recent
posts, persona-drafted responses, Telegram approval before anything
publishes. Publishing replies to the inbound reply's Graph media id via the
proven container → threads_publish flow. Browser automation is never used;
external-root replies are never attempted.

State machine on `threads_own_reply_engagement` (migration 010):

```text
discovered -> pending_approval -> approved -> posting -> posted
                               -> rejected (terminal)
                               -> ignored  (terminal)
                               -> failed   (permanent errors; transient
                                            errors return to approved for
                                            retry, max 5 attempts)
```

Commands:

```bash
# discover new replies (dedup by reply id); --propose also drafts + moves
# to pending_approval. --dry-run touches nothing.
.venv/bin/threads-operator own-replies scan --account syaqir [--limit 10] [--propose] [--dry-run]

.venv/bin/threads-operator own-replies list --account syaqir [--status pending_approval]

# decisions (all require an open state; CAS-guarded)
.venv/bin/threads-operator own-replies approve --account syaqir --id <row-id>
.venv/bin/threads-operator own-replies edit    --account syaqir --id <row-id> --text <replacement>
.venv/bin/threads-operator own-replies reject  --account syaqir --id <row-id>
.venv/bin/threads-operator own-replies ignore  --account syaqir --id <row-id>

# publish approved rows only (claim-first CAS; THREADS_ENGAGEMENT_ENABLED
# gate; transient vs permanent error separation)
.venv/bin/threads-operator own-replies publish-approved --account syaqir [--id <row-id>] [--dry-run]
```

Telegram card (callback data `ownreply:<action>:<id>`, handled by
`OwnReplyDispatcher`):

```text
💬 NEW THREADS REPLY
Our post: <preview>
From: @username
Their reply: ...
Suggested response (n chars): ...
[Approve] [Edit] [Reject] [Ignore]
```

Watchdog: `~/.hermes/scripts/own_replies_watchdog_5m.py`, every 5 minutes
(cron job "Threads Own-Replies Watchdog (syaqir)").

## LLM configuration (shared)

`content_generation.resolve_providers()` resolves in order:

1. `THREADS_OPERATOR_LLM_BASE_URL` / `_API_KEY` / `_MODEL` env overrides;
2. primary `model` block of `~/.hermes/config.yaml`;
3. `fallback_providers`;
4. `custom_providers` (with `key_env` indirection).

All providers must speak OpenAI chat-completions. First success wins;
failures fall through with a log line. Persona loading is account-scoped:
`personas/<account-key>.md`, exact match, no cross-account fallback —
missing persona stops generation loudly.

## Safety invariants

- Nothing publishes without a recorded Telegram approval transition.
- Approve → enqueue (A) / publish (B) is idempotent: CAS transitions and
  `used_in_queue_id` / `(account_key, reply_id)` uniqueness prevent
  duplicates even under double-taps or overlapping workers.
- API errors never cause duplicate replies: claim-first CAS
  (`approved -> posting`) means a second worker cannot race a publish.
- Transient errors (5xx, rate limit, timeouts) return the row to
  `approved` for a later tick; permanent errors (auth/permission) burn it
  to `failed` with the exact API error in `last_error` and alert Telegram.
- The 500-character Threads limit is enforced on generated drafts and
  Telegram edits alike.
