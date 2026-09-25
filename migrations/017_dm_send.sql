-- 017_dm_send.sql — Task 2D: safe browser-based Threads DM sender.
--
-- Adds the deterministic send lifecycle on top of 015/016:
--   * new status "send_uncertain" for ambiguous post-click delivery
--   * claim_id + last_attempt_at for atomic worker claiming + audit
--   * send confirmation reference / evidence + text hash
--   * structured failure_category
--
-- 015 already provides: claimed_at, sent_at, external_dm_id, last_error,
-- attempt_count. 016 provides drafted_at/edited_at/approval_message_ref/
-- approved_by/reject_reason. This migration only ADDS what the send path needs.
--
-- Idempotent: guarded by IF NOT EXISTS / drop-if-exists. Service-role only;
-- no new RLS surface beyond the existing table policies.

begin;

-- 1) Extend the status CHECK to allow send_uncertain.
--    The CHECK is unnamed in 015, so drop by constraint scan then re-add.
do $$
declare c record;
begin
  for c in
    select conname from pg_constraint
    where conrelid = 'public.threads_dm_opportunities'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) ilike '%status%in%approved%'
  loop
    execute format('alter table public.threads_dm_opportunities drop constraint %I', c.conname);
  end loop;
end $$;

alter table public.threads_dm_opportunities
  add constraint threads_dm_opportunities_status_check
  check (status in (
    'detected','drafted','awaiting_approval','approved',
    'sending','sent','rejected','cancelled','send_uncertain'
  ));

-- 2) Atomic claim + send audit fields.
alter table public.threads_dm_opportunities
  add column if not exists claim_id text,
  add column if not exists last_attempt_at timestamptz,
  add column if not exists confirmation_ref text,
  add column if not exists confirmation_evidence text,
  add column if not exists sent_text_hash text,
  add column if not exists failure_category text;

-- 3) Index so the sender/worker can find the next claimable approved row and
--    reconcile uncertain sends cheaply (account-scoped).
create index if not exists threads_dm_opportunities_send_queue_idx
  on public.threads_dm_opportunities (account_key, status, id)
  where status in ('approved','send_uncertain');

commit;
