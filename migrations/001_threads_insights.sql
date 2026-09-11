-- Historical Threads Insights schema.
-- Raw snapshot tables are append-only sources of truth. Derived rollups can be rebuilt.

create extension if not exists pgcrypto;

create table if not exists threads_account_snapshots (
    id uuid primary key default gen_random_uuid(),
    captured_at timestamptz not null,
    account_id text not null,
    views bigint,
    likes bigint,
    replies bigint,
    reposts bigint,
    quotes bigint,
    clicks bigint,
    followers_count bigint,
    created_at timestamptz not null default now()
);

create index if not exists idx_threads_account_snapshots_account_time
    on threads_account_snapshots (account_id, captured_at desc);

create table if not exists threads_post_snapshots (
    id uuid primary key default gen_random_uuid(),
    captured_at timestamptz not null,
    account_id text not null,
    post_id text not null,
    published_at timestamptz,
    post_age_minutes numeric,
    views bigint,
    likes bigint,
    replies bigint,
    reposts bigint,
    quotes bigint,
    shares bigint,
    created_at timestamptz not null default now()
);

create index if not exists idx_threads_post_snapshots_post_time
    on threads_post_snapshots (account_id, post_id, captured_at desc);

create index if not exists idx_threads_post_snapshots_age
    on threads_post_snapshots (post_age_minutes);

create table if not exists threads_daily_rollups (
    date date not null,
    account_id text not null,
    followers_start bigint,
    followers_end bigint,
    followers_gained bigint,
    follower_growth_pct numeric,
    profile_views_gained bigint,
    views_gained bigint,
    likes_gained bigint,
    replies_gained bigint,
    reposts_gained bigint,
    quotes_gained bigint,
    posts_published integer,
    rebuilt_at timestamptz not null default now(),
    primary key (date, account_id)
);

-- SQL-created tables in Supabase do not automatically gain the same RLS
-- protection as tables created through the dashboard. Keep these private by
-- default; the server-side service-role collector can still access them.
alter table threads_account_snapshots enable row level security;
alter table threads_post_snapshots enable row level security;
alter table threads_daily_rollups enable row level security;

comment on table threads_account_snapshots is
    'Append-only raw account Insights snapshots. Do not overwrite historical rows.';
comment on table threads_post_snapshots is
    'Append-only raw post Insights snapshots. Missing metrics remain NULL.';
comment on table threads_daily_rollups is
    'Derived/rebuildable summaries computed from raw snapshots.';
