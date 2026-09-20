-- Durable retry state for the deterministic publish queue.
--
-- Why: a poisoned row (e.g. Meta persistently rejects over-length text with a
-- 500, or a non-auto-retryable content error) used to stay `approved` forever
-- and, because the worker selects the oldest due approved row first, it blocked
-- every later scheduled post (head-of-line starvation). Persisting per-row
-- attempt state + a backoff timestamp lets the worker skip a row that is not
-- yet due for retry and eventually fail it after a max-attempt budget, so later
-- eligible rows are never starved.
--
-- These columns are additive and default-safe: existing rows get
-- attempt_count=0 / next_retry_at=NULL, which keeps them eligible immediately
-- (next_retry_at IS NULL is treated as "not in backoff").

alter table public.threads_publish_queue
    add column if not exists attempt_count integer not null default 0
        check (attempt_count >= 0);

alter table public.threads_publish_queue
    add column if not exists last_attempt_at timestamptz;

alter table public.threads_publish_queue
    add column if not exists next_retry_at timestamptz;

-- Selection helper index: the worker looks for approved rows that are due and
-- not in backoff (next_retry_at is null or <= now). Partial index keeps it small.
create index if not exists idx_threads_publish_queue_retry
    on public.threads_publish_queue (account_key, status, scheduled_at, id)
    where status = 'approved';

comment on column public.threads_publish_queue.attempt_count is
    'Persisted publish-attempt counter; incremented on every claim attempt.';
comment on column public.threads_publish_queue.last_attempt_at is
    'UTC timestamp of the most recent publish attempt.';
comment on column public.threads_publish_queue.next_retry_at is
    'UTC backoff gate. NULL = eligible now; future = in backoff, skipped by due-row selection.';
