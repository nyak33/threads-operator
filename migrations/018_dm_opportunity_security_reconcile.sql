-- Migration 018: reconcile least-privilege grants for DM opportunities.
--
-- Supabase default privileges can leave service_role with broader table rights
-- than the explicit GRANT in migration 015. Task #2 only requires SELECT,
-- INSERT, and UPDATE on threads_dm_opportunities. Preserve sequence access for
-- identity inserts, and keep anon/authenticated fully revoked.
--
-- Idempotent and non-destructive.

begin;

revoke all on table public.threads_dm_opportunities from service_role;
grant select, insert, update on table public.threads_dm_opportunities to service_role;

revoke all on sequence public.threads_dm_opportunities_id_seq from service_role;
grant usage, select on sequence public.threads_dm_opportunities_id_seq to service_role;

revoke all on table public.threads_dm_opportunities from public;
revoke all on table public.threads_dm_opportunities from anon, authenticated;

commit;
