-- Upgrade an already-existing single-account Activity table without dropping history.
-- Existing rows cannot be mapped to a deployment-local account key generically, so
-- they are preserved under account_key='legacy'. Rename that key to the intended
-- local account key before enabling multi-account Activity writes if appropriate.

alter table public.threads_activity_events
    add column if not exists account_key text;

update public.threads_activity_events
set account_key = 'legacy'
where account_key is null;

alter table public.threads_activity_events
    alter column account_key set not null;

alter table public.threads_activity_events
    drop constraint if exists threads_activity_events_fallback_fingerprint_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.threads_activity_events'::regclass
          AND conname = 'threads_activity_events_account_key_fallback_fingerprint_key'
    ) THEN
        ALTER TABLE public.threads_activity_events
            ADD CONSTRAINT threads_activity_events_account_key_fallback_fingerprint_key
            UNIQUE (account_key, fallback_fingerprint);
    END IF;
END
$$;

drop index if exists public.idx_threads_activity_events_notification;
drop index if exists public.idx_threads_activity_events_datetime;
drop index if exists public.idx_threads_activity_events_post_confidence;

create index idx_threads_activity_events_notification
    on public.threads_activity_events (account_key, notification_id, collected_at desc);
create index idx_threads_activity_events_datetime
    on public.threads_activity_events (account_key, notification_datetime desc);
create index idx_threads_activity_events_post_confidence
    on public.threads_activity_events (account_key, matched_post_id, match_confidence);

drop view if exists public.threads_follows_from_post_summary;

create view public.threads_follows_from_post_summary
with (security_invoker = true) as
with latest_notification_state as (
    select distinct on (account_key, notification_id)
        account_key,
        notification_id,
        follow_count,
        matched_post_id,
        matched_permalink,
        match_confidence,
        collected_at
    from public.threads_activity_events
    order by account_key, notification_id, collected_at desc, id desc
)
select
    account_key,
    matched_post_id as post_id,
    max(matched_permalink) as permalink,
    coalesce(sum(follow_count) filter (where match_confidence = 'high'), 0)::bigint
        as high_confidence_follows,
    coalesce(sum(follow_count) filter (where match_confidence = 'medium'), 0)::bigint
        as medium_confidence_follows,
    coalesce(sum(follow_count) filter (where match_confidence = 'low'), 0)::bigint
        as low_confidence_follows,
    coalesce(sum(follow_count) filter (where match_confidence in ('high','medium')), 0)::bigint
        as follows_from_post_activity,
    count(*)::bigint as notification_rows,
    max(collected_at) as updated_at
from latest_notification_state
where matched_post_id is not null
group by account_key, matched_post_id;

revoke all on table public.threads_follows_from_post_summary from public, anon, authenticated;
grant select on table public.threads_follows_from_post_summary to service_role;
