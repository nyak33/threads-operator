-- Collision-safe historical Threads Insights snapshot tables used by threads-operator.

create extension if not exists pgcrypto;

create table if not exists threads_account_insights_snapshots (
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

create index if not exists idx_threads_account_insights_snapshots_account_time
    on threads_account_insights_snapshots (account_id, captured_at desc);

create table if not exists threads_post_insights_snapshots (
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

create index if not exists idx_threads_post_insights_snapshots_post_time
    on threads_post_insights_snapshots (account_id, post_id, captured_at desc);

create index if not exists idx_threads_post_insights_snapshots_age
    on threads_post_insights_snapshots (post_age_minutes);

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

alter table threads_account_insights_snapshots enable row level security;
alter table threads_post_insights_snapshots enable row level security;
alter table threads_daily_rollups enable row level security;

grant usage on schema public to anon, authenticated, service_role;
grant select, insert on threads_account_insights_snapshots to anon, authenticated, service_role;
grant select, insert on threads_post_insights_snapshots to anon, authenticated, service_role;
grant select, insert, update on threads_daily_rollups to anon, authenticated, service_role;

comment on table threads_account_insights_snapshots is
    'Append-only raw account Insights snapshots (threads-operator collector). Do not overwrite historical rows.';
comment on table threads_post_insights_snapshots is
    'Append-only raw post Insights snapshots (threads-operator collector). Missing metrics remain NULL.';
comment on table threads_daily_rollups is
    'Derived/rebuildable summaries computed from raw snapshots.';
