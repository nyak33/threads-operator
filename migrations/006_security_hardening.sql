-- Forward-only privilege hardening for operator-owned Insights tables.
-- Keep client roles out; server-side service_role workers retain only required access.

revoke all on table public.threads_account_insights_snapshots from public;
revoke all on table public.threads_account_insights_snapshots from anon, authenticated;
grant select, insert on table public.threads_account_insights_snapshots to service_role;

revoke all on table public.threads_post_insights_snapshots from public;
revoke all on table public.threads_post_insights_snapshots from anon, authenticated;
grant select, insert on table public.threads_post_insights_snapshots to service_role;

revoke all on table public.threads_daily_rollups from public;
revoke all on table public.threads_daily_rollups from anon, authenticated;
grant select, insert, update on table public.threads_daily_rollups to service_role;

-- Legacy snapshot tables may still exist on upgraded deployments.
revoke all on table public.threads_account_snapshots from public;
revoke all on table public.threads_account_snapshots from anon, authenticated;
grant select, insert on table public.threads_account_snapshots to service_role;

revoke all on table public.threads_post_snapshots from public;
revoke all on table public.threads_post_snapshots from anon, authenticated;
grant select, insert on table public.threads_post_snapshots to service_role;
