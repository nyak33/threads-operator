-- Migration 012: 24-hour approval timeout + durable content backlog for Workflow A.
--
-- Scope: standalone own-post drafts from trend candidates. Own-reply engagement
-- (threads_own_reply_engagement) is NOT touched.
--
-- Idempotent: safe to re-run. Existing rows keep their current status; only the
-- schema CHECK and two timestamp columns are added. The new 'backlog' status is
-- terminal for the approval timeout path; reusing backlog content moves the row
-- through 'approved' -> 'queued' using the existing enqueue_draft path.

-- ---------------------------------------------------------------
-- 1) Add 'backlog' to the threads_trend_candidates status CHECK.
--    Drop + recreate the constraint only when needed.
-- ---------------------------------------------------------------
do $$
declare
    conname text;
    condef text;
begin
    select con.conname, pg_get_constraintdef(con.oid) into conname, condef
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where rel.relname = 'threads_trend_candidates'
      and nsp.nspname = 'public'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) ilike '%status%';

    -- Only recreate if the constraint exists and does not already list 'backlog'.
    if conname is not null and condef is not null and condef not like '%backlog%' then
        execute format('alter table public.threads_trend_candidates drop constraint %I', conname);

        alter table public.threads_trend_candidates
            add constraint threads_trend_candidates_status_check check (
                status in (
                    'discovered', 'drafted', 'pending_approval', 'approved',
                    'queued', 'rejected', 'skipped', 'posted', 'failed',
                    'backlog', 'discarded',
                    'reviewed', 'used', 'stale'
                )
            );
    end if;
end $$;

-- If no status constraint existed at all (unlikely), add it explicitly.
do $$
declare
    conname text;
begin
    select con.conname into conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where rel.relname = 'threads_trend_candidates'
      and nsp.nspname = 'public'
      and con.contype = 'c'
      and pg_get_constraintdef(con.oid) ilike '%status%';

    if conname is null then
        alter table public.threads_trend_candidates
            add constraint threads_trend_candidates_status_check check (
                status in (
                    'discovered', 'drafted', 'pending_approval', 'approved',
                    'queued', 'rejected', 'skipped', 'posted', 'failed',
                    'backlog', 'discarded',
                    'reviewed', 'used', 'stale'
                )
            );
    end if;
end $$;

-- ---------------------------------------------------------------
-- 2) Add approval timing columns if missing.
-- ---------------------------------------------------------------
alter table public.threads_trend_candidates
    add column if not exists approval_sent_at timestamptz null;

alter table public.threads_trend_candidates
    add column if not exists timed_out_at timestamptz null;

-- ---------------------------------------------------------------
-- 3) Index for the timeout worker: pending_approval rows older than 24h.
-- ---------------------------------------------------------------
create index if not exists threads_trend_candidates_pending_approval_at_idx
    on public.threads_trend_candidates (target_account_id, approval_sent_at)
    where status = 'pending_approval';

-- Index for listing active backlog content per account.
create index if not exists threads_trend_candidates_backlog_account_idx
    on public.threads_trend_candidates (target_account_id, timed_out_at desc)
    where status = 'backlog';

comment on column public.threads_trend_candidates.approval_sent_at is
    'When the Telegram approval card was first sent for this Workflow A draft.';

comment on column public.threads_trend_candidates.timed_out_at is
    'When this pending_approval draft was moved to backlog by the timeout worker.';
