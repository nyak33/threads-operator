# Task 2D — Safe Browser-Based Threads DM Sender: Live Readiness

Status: **code complete, deterministic suite green.** No live DM has been sent.
This document lists the manual production actions required before Task 2D can
send real DMs, and the controlled live-verification procedure.

## What was built

- `src/threads_operator/dm_send.py` — deterministic send state machine:
  eligibility gate, atomic claim, account/recipient/exact-text verification,
  send + confirmation, `send_uncertain`, reconciliation, failure categories.
- `src/threads_operator/dm_browser.py` — live CDP adapter (`ThreadsDMPage`)
  driving the persistent authenticated Chromium via `browser_cdp` (raw
  websocket, no Playwright/Selenium). Recon-grounded selectors, fail-closed.
- `migrations/017_dm_send.sql` — `send_uncertain` status + claim/audit fields.
- CLI: `own-replies dm {send, send-next, send-queue, inspect, reconcile}`.
- Watchdog: `run_dm_send_pass()` in `scripts/own_replies_watchdog_5m.py` sends
  ONE approved DM per 5-minute pass and notifies Telegram on sent/failed/
  uncertain. The approval callback never blocks on the browser.
- Telegram notifications reuse the existing bot token/chat id env vars.

## Recon-grounded Threads web DM flow (verified 2026-09-25)

- DM surface: `https://www.threads.com/messages` (NOT `/direct/...` — 404).
- First-visit intro dialog ("Direct messages have arrived on web") → Continue.
- Recipient search: `<input placeholder="Search">`; recipient row is a
  `div[role="button"]` whose `innerText` **first line = exact username**.
- Conversation URL becomes `/messages/t/<thread_id>` (the `external_dm_id`).
- Composer: `<div contenteditable="true" role="textbox">`; Enter sends.

## Manual production actions (NOT done automatically)

1. **Apply migrations in order** to production Supabase. **VERIFIED 2026-09-25
   (Task 2E Phase 3 read-back): production has NO DM schema at all** —
   `threads_dm_opportunities` is ABSENT and `threads_own_reply_engagement` has
   NO context/intent columns. Therefore **015, 016, AND 017 are ALL unapplied**
   (not just 016/017 as previously assumed). Apply in order:
   - `015_reply_context_intent.sql` (creates `threads_dm_opportunities` + context/intent cols)
   - `016_dm_approval_workflow.sql`
   - `017_dm_send.sql`
   This host has no psql / Supabase CLI / DB connection string — apply via the
   Supabase dashboard SQL editor. Order matters: each builds on the prior.
2. **Restart the Hermes gateway** so the Task 2C `dmopp:` adapter route is live.
   The running process (started 17:59:51 +08) predates the route commit
   `1485c5a5` (created 20:01:53), so the route is NOT loaded. The gateway blocks
   self-restart from inside its own process — run from a separate shell:
   `systemctl --user restart hermes-gateway.service` (or `hermes gateway restart`).
3. **Confirm the persistent browser session is logged in** to the correct
   Threads account and not showing any challenge. VERIFIED 2026-09-25 (Phase 5):
   CDP reachable (Chrome/145), `/messages` loads, NO CAPTCHA/2FA/login/warning,
   persistent profile `~/.threads-operator/browser-profiles/syaqir`. Logged-in
   username = **`syaqir_sharani`** (NOT the account_key label `syaqir`).
4. **`THREADS_USERNAME` is set** in `accounts/syaqir.env` to `syaqir_sharani`
   (done 2026-09-25, commit `c5b6b4f`) so account verification compares the
   browser login against the real Threads username, not the account_key label.
5. **Set the Telegram env vars** for the watchdog notifications if not already
   exported: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Account-verification fix (Task 2E, commit c5b6b4f)

Phase 5 live readiness exposed a real pre-live defect: the sender compared the
browser login against the `account_key` operator label (`syaqir`) instead of the
real Threads username (`syaqir_sharani`), so the legitimate account would have
failed `account_mismatch` and blocked every send. Fixed: `THREADS_USERNAME`
config carries the real handle; the WHOAMI probe now reads the logged-in account
from the frequent `/@<user>` own-profile links (was matching `/search`).
Verified live: probe returns `syaqir_sharani`, matches `THREADS_USERNAME`.

## Controlled live-verification procedure (requires an approved recipient)

Do NOT send unsolicited DMs. Use a controlled recipient approved by the operator
(e.g. a second account the operator owns). End-to-end proof path:

1. Controlled Threads comment on an operator post → lead detected.
2. `own-replies dm draft --id <id>` → Hermes draft persisted, `awaiting_approval`.
3. Telegram approval card → Approve (persists `approved` + exact text).
4. `own-replies dm send --id <id>` → browser claims, verifies account +
   recipient, inserts exact text, sends once, confirms, marks `sent`.
5. Confirm in Supabase: `status=sent`, `external_dm_id`, `sent_at`,
   `confirmation_ref` set; Telegram "DM SENT" notification received.
6. Negative checks: re-running `dm send --id <id>` on the sent row must refuse;
   a row whose browser account ≠ opportunity account must fail `account_mismatch`.

## Safety invariants enforced by the suite

- Only `status=approved` + approved-text + target-username rows are eligible.
- Atomic claim (CAS on `approved`) prevents concurrent/duplicate sends.
- Exact canonical username match; partial/ambiguous results never send.
- Composer text must equal approved text before Send; mismatch aborts pre-click.
- Click ≠ delivery; confirmation read decides `sent`, else `send_uncertain`.
- `send_uncertain` never auto-resends; `reconcile` resolves to sent / approved /
  still-uncertain.
- A confirmed `sent` row can never be sent again.
- Login/2FA/CAPTCHA/checkpoint and account mismatch fail closed.

## Test results (deterministic)

- threads-operator: **758 passed, 1 skipped** (baseline 698 + 60 new).
- Task 2A/2B/2C suites remain green; public reply workflow green.
