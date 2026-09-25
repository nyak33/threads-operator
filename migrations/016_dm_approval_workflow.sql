-- Migration 016 — Task 2C: Telegram DM Approval Workflow
--
-- Adds the approval-bookkeeping columns the approval layer needs that
-- migration 015 did not create:
--
--   drafted_at            timestamptz  when the exact draft was persisted
--   edited_at             timestamptz  last human edit timestamp
--   approval_message_ref  text         Telegram "chat_id:message_id" of the
--                                      active approval card (idempotency: a
--                                      row that already has one is not
--                                      re-sent a duplicate card)
--   approved_by           text         Telegram user id that approved
--   reject_reason         text         optional rejection note
--
-- Idempotent: safe to re-run (ADD COLUMN IF NOT EXISTS).

alter table public.threads_dm_opportunities
    add column if not exists drafted_at timestamptz;

alter table public.threads_dm_opportunities
    add column if not exists edited_at timestamptz;

alter table public.threads_dm_opportunities
    add column if not exists approval_message_ref text;

alter table public.threads_dm_opportunities
    add column if not exists approved_by text;

alter table public.threads_dm_opportunities
    add column if not exists reject_reason text;

-- Index for the watchdog query (awaiting_approval without a card yet).
create index if not exists threads_dm_opportunities_awaiting_card_idx
    on public.threads_dm_opportunities (account_key, status)
    where status in ('detected', 'drafted', 'awaiting_approval');
