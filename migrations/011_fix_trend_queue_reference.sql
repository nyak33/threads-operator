-- Migration 011: align threads_trend_candidates.used_in_queue_id with the publish queue.
--
-- Workflow A (trend candidate -> original own post) enqueues approved drafts into
-- threads_publish_queue (bigint primary key). The live schema had
-- used_in_queue_id as a UUID referencing threads_content_queue, which caused
-- trend-engagement approve to fail with:
--   "invalid input syntax for type uuid" / foreign-key violation.
--
-- This migration fixes the column type and foreign key so the code, tests, and
-- docs all agree. Only synthetic/test rows had non-null values; real Workflow A
-- had not been live yet.

do $$
declare
    fk_name text;
begin
    -- Find and drop the existing foreign key on used_in_queue_id (if any).
    select con.conname into fk_name
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where rel.relname = 'threads_trend_candidates'
      and nsp.nspname = 'public'
      and con.contype = 'f'
      and con.conrelid = 'public.threads_trend_candidates'::regclass
      and con.conkey @> array[
          (select attnum from pg_attribute
           where attrelid = 'public.threads_trend_candidates'::regclass
             and attname = 'used_in_queue_id')
      ];

    if fk_name is not null then
        execute format('alter table public.threads_trend_candidates drop constraint %I', fk_name);
    end if;
end $$;

-- Drop the dependent unique index before altering the column.
drop index if exists public.threads_trend_candidates_used_queue_uidx;

-- Clear any synthetic/test values so the type change is safe.
update public.threads_trend_candidates
   set used_in_queue_id = null
 where used_in_queue_id is not null;

-- Align the column with threads_publish_queue.id (bigint).
alter table public.threads_trend_candidates
    alter column used_in_queue_id type bigint
    using (used_in_queue_id::bigint);

-- Add the correct foreign key to the publish queue.
alter table public.threads_trend_candidates
    add constraint threads_trend_candidates_used_in_queue_id_fkey
    foreign key (used_in_queue_id)
    references public.threads_publish_queue(id)
    on delete set null;

-- Recreate the idempotency unique index.
create unique index if not exists threads_trend_candidates_used_queue_uidx
    on public.threads_trend_candidates (used_in_queue_id)
    where used_in_queue_id is not null;
