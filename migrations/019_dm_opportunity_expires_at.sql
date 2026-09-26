-- Migration 019: add explicit DM opportunity expiry deadline.
--
-- Task 2C/2D code uses expires_at as the deadline for approval/send expiry
-- checks, while expired_at records when the terminal expiry transition
-- actually happened. Production schema previously had expired_at only, making
-- DM opportunity inserts and expiry queries fail through PostgREST.
--
-- Idempotent and non-destructive.

alter table public.threads_dm_opportunities
    add column if not exists expires_at timestamptz null;

create index if not exists threads_dm_opportunities_expiry_idx
    on public.threads_dm_opportunities (account_key, expires_at)
    where status in ('detected','drafted','awaiting_approval','approved','sending');
