-- Migration 014: reconcile fresh-install RLS / privilege posture with production.
--
-- DEFECT (externally verified 2026-09-23, fresh Supabase replay of 013..012):
--   A brand-new database did NOT reproduce production's Row-Level-Security posture.
--   RLS stayed DISABLED and default privileges remained on:
--     threads_trend_candidates, threads_engagement_config, threads_gateway_keys,
--     threads_posts, threads_inbound_replies, threads_post_metric_snapshots,
--     threads_own_reply_engagement.
--   Supabase Security Advisor reported ERROR: threads_own_reply_engagement is
--   public but RLS is not enabled (and anon/authenticated held privileges on it).
--
-- ROOT CAUSE:
--   * 013 revokes client roles but never enables RLS (so revokes alone leave the
--     tables flagged "public" by the advisor and without RLS enforcement).
--   * 010 creates threads_own_reply_engagement with NO grant/revoke/RLS section,
--     so it inherits permissive defaults (anon/authenticated keep privileges).
--
-- INTENDED POSTURE (operator architecture): these tables are server-side only.
--   * RLS ENABLED on all of them.
--   * anon / authenticated: NO privileges (fully locked out).
--   * service_role: retains the access each table already uses (see per-table
--     grants below — they mirror the 013 grants exactly, plus threads_own_reply_engagement).
--   * No client-facing (anon/authenticated) policies are created — none required.
--   This preserves production behaviour; it does NOT add permissive client policies.
--
-- PER-TABLE service_role GRANTS (mirrors 013 exactly; own_reply_engagement = S/I/U,
--   the Workflow B lifecycle never deletes rows):
--   threads_trend_candidates       S/I/U/D   (013:241)
--   threads_engagement_config      S/I/U     (013:245)
--   threads_gateway_keys           S/I/U/D   (013:249)
--   threads_posts                  S/I/U     (013:253)
--   threads_inbound_replies        S/I/U     (013:257)
--   threads_post_metric_snapshots  S/I       (013:261; append-only)
--   threads_own_reply_engagement   S/I/U     (Workflow B lifecycle; no deletes)
--
-- SAFETY: additive + idempotent. enable row level security is a no-op if already
-- enabled; revokes/grants are idempotent; the service_role policy is dropped-then-
-- created (drop policy if exists) so re-runs converge. No data is read or mutated.
-- On the existing production database this is posture-preserving (RLS already
-- enabled, client roles already revoked); it only adds the service_role policy
-- where absent, matching how the service role already effectively operates.

do $$
begin
    if to_regclass('public.threads_trend_candidates') is not null then
        alter table public.threads_trend_candidates enable row level security;
        revoke all on table public.threads_trend_candidates from public;
        revoke all on table public.threads_trend_candidates from anon, authenticated;
        grant select, insert, update, delete on table public.threads_trend_candidates to service_role;
        drop policy if exists "service_role_all_threads_trend_candidates" on public.threads_trend_candidates;
        create policy "service_role_all_threads_trend_candidates"
            on public.threads_trend_candidates for all to service_role using (true) with check (true);
    end if;

    if to_regclass('public.threads_engagement_config') is not null then
        alter table public.threads_engagement_config enable row level security;
        revoke all on table public.threads_engagement_config from public;
        revoke all on table public.threads_engagement_config from anon, authenticated;
        grant select, insert, update on table public.threads_engagement_config to service_role;
        drop policy if exists "service_role_all_threads_engagement_config" on public.threads_engagement_config;
        create policy "service_role_all_threads_engagement_config"
            on public.threads_engagement_config for all to service_role using (true) with check (true);
    end if;

    if to_regclass('public.threads_gateway_keys') is not null then
        alter table public.threads_gateway_keys enable row level security;
        revoke all on table public.threads_gateway_keys from public;
        revoke all on table public.threads_gateway_keys from anon, authenticated;
        grant select, insert, update, delete on table public.threads_gateway_keys to service_role;
        drop policy if exists "service_role_all_threads_gateway_keys" on public.threads_gateway_keys;
        create policy "service_role_all_threads_gateway_keys"
            on public.threads_gateway_keys for all to service_role using (true) with check (true);
    end if;

    if to_regclass('public.threads_posts') is not null then
        alter table public.threads_posts enable row level security;
        revoke all on table public.threads_posts from public;
        revoke all on table public.threads_posts from anon, authenticated;
        grant select, insert, update on table public.threads_posts to service_role;
        drop policy if exists "service_role_all_threads_posts" on public.threads_posts;
        create policy "service_role_all_threads_posts"
            on public.threads_posts for all to service_role using (true) with check (true);
    end if;

    if to_regclass('public.threads_inbound_replies') is not null then
        alter table public.threads_inbound_replies enable row level security;
        revoke all on table public.threads_inbound_replies from public;
        revoke all on table public.threads_inbound_replies from anon, authenticated;
        grant select, insert, update on table public.threads_inbound_replies to service_role;
        drop policy if exists "service_role_all_threads_inbound_replies" on public.threads_inbound_replies;
        create policy "service_role_all_threads_inbound_replies"
            on public.threads_inbound_replies for all to service_role using (true) with check (true);
    end if;

    if to_regclass('public.threads_post_metric_snapshots') is not null then
        alter table public.threads_post_metric_snapshots enable row level security;
        revoke all on table public.threads_post_metric_snapshots from public;
        revoke all on table public.threads_post_metric_snapshots from anon, authenticated;
        grant select, insert on table public.threads_post_metric_snapshots to service_role;
        drop policy if exists "service_role_all_threads_post_metric_snapshots" on public.threads_post_metric_snapshots;
        create policy "service_role_all_threads_post_metric_snapshots"
            on public.threads_post_metric_snapshots for all to service_role using (true) with check (true);
    end if;

    if to_regclass('public.threads_own_reply_engagement') is not null then
        alter table public.threads_own_reply_engagement enable row level security;
        revoke all on table public.threads_own_reply_engagement from public;
        revoke all on table public.threads_own_reply_engagement from anon, authenticated;
        grant select, insert, update on table public.threads_own_reply_engagement to service_role;
        drop policy if exists "service_role_all_threads_own_reply_engagement" on public.threads_own_reply_engagement;
        create policy "service_role_all_threads_own_reply_engagement"
            on public.threads_own_reply_engagement for all to service_role using (true) with check (true);
    end if;
end
$$;

comment on table public.threads_own_reply_engagement is
    'Approval-gated responses to replies under our own Threads posts (Workflow B). '
    'Nothing publishes until status transitions pending_approval -> approved via Telebot. '
    'RLS enabled by migration 014: service_role only; anon/authenticated locked out.';
