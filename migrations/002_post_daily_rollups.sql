-- ============================================================
-- threads-operator migration 002 — PASTE KE SUPABASE SQL EDITOR
-- Project: ztoxaksrdbcgceyulzwm
-- STATUS 2026-09-23: NOT YET APPLIED on production (verified: table absent
-- from the live PostgREST schema, GET 404 PGRST205). scripts/rebuild_rollups.py
-- writes here and will fail until this migration is run. Apply before
-- scheduling rollup rebuilds.
-- ADDITIVE ONLY: create table/indexes/grants/comments sahaja.
-- Tiada DROP, tiada ALTER pada table sedia ada.
-- threads_daily_rollups (account-level) TIDAK disentuh.
-- Raw snapshots + legacy tables TIDAK disentuh.
-- Safe to re-run (IF NOT EXISTS).
-- ============================================================

create extension if not exists pgcrypto;

create table if not exists threads_post_daily_rollups (
    date date not null,
    account_id text not null,
    post_id text not null,
    published_at timestamptz,
    snapshot_count integer not null default 0,
    first_captured_at timestamptz,
    last_captured_at timestamptz,
    views_start bigint,
    views_end bigint,
    views_gained bigint,
    likes_start bigint,
    likes_end bigint,
    likes_gained bigint,
    replies_gained bigint,
    reposts_gained bigint,
    quotes_gained bigint,
    shares_gained bigint,
    engagement_gained bigint,
    max_view_velocity_per_hour numeric,
    peak_velocity_at timestamptz,
    age_minutes_start numeric,
    age_minutes_end numeric,
    anomalies text,
    rebuilt_at timestamptz,
    constraint threads_post_daily_rollups_pkey primary key (date, account_id, post_id),
    constraint threads_post_daily_rollups_snapshot_count_nonneg check (snapshot_count >= 0)
);

create index if not exists idx_tpdr_account_date
    on threads_post_daily_rollups (account_id, date desc);
create index if not exists idx_tpdr_date
    on threads_post_daily_rollups (date desc);

alter table threads_post_daily_rollups enable row level security;

drop policy if exists "service_role_all_threads_post_daily_rollups" on threads_post_daily_rollups;
create policy "service_role_all_threads_post_daily_rollups"
    on threads_post_daily_rollups for all to service_role;

grant select, insert, update, delete on threads_post_daily_rollups to service_role;

comment on table threads_post_daily_rollups is
    'Daily post-level aggregates rebuilt from threads_post_insights_snapshots (source of truth). Rebuildable; keyed (date, account_id, post_id). MYT calendar days.';
comment on column threads_post_daily_rollups.snapshot_count is
    'Usable snapshots within the MYT day (all-NULL rows dropped).';
comment on column threads_post_daily_rollups.views_start is
    'Opening views: last snapshot before the day, else first in-day value. NULL if no usable data.';
comment on column threads_post_daily_rollups.views_gained is
    'views_end - views_start; NULL if either side unknown or a reset was detected (see anomalies).';
comment on column threads_post_daily_rollups.max_view_velocity_per_hour is
    'Max (views delta / hours) across consecutive in-day snapshot pairs.';
comment on column threads_post_daily_rollups.peak_velocity_at is
    'captured_at of the interval end that produced max velocity.';
comment on column threads_post_daily_rollups.age_minutes_start is
    'post_age_minutes of first in-day snapshot (same row as first_captured_at).';
comment on column threads_post_daily_rollups.age_minutes_end is
    'post_age_minutes of last in-day snapshot (same row as last_captured_at).';
comment on column threads_post_daily_rollups.engagement_gained is
    'Sum of replies+reposts+quotes+shares gained; NULL if no component known.';
comment on column threads_post_daily_rollups.anomalies is
    'Semicolon notes: negative deltas, counter resets (non-monotonic), dropped all-NULL snapshots.';
