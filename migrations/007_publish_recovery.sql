-- Global bounded recovery metadata for deterministic Threads publishing.
-- Retry state is shared by all campaigns using threads_publish_queue.

alter table public.threads_publish_queue
    add column if not exists attempt_count integer not null default 0
        check (attempt_count >= 0),
    add column if not exists next_retry_at timestamptz,
    add column if not exists retry_deadline_at timestamptz,
    add column if not exists last_error_meta jsonb;

alter table public.threads_publish_queue
    drop constraint if exists threads_publish_queue_status_check;

alter table public.threads_publish_queue
    add constraint threads_publish_queue_status_check check (
        status in (
            'draft',
            'approved',
            'posting',
            'retrying',
            'posted',
            'failed',
            'needs_attention'
        )
    );

create index if not exists idx_threads_publish_queue_retry_due
    on public.threads_publish_queue (
        account_key,
        status,
        next_retry_at,
        retry_deadline_at,
        id
    );

comment on column public.threads_publish_queue.attempt_count is
    'Number of queue-level automatic recovery attempts after the initial publish attempt.';
comment on column public.threads_publish_queue.next_retry_at is
    'Earliest time a retrying row may be claimed again.';
comment on column public.threads_publish_queue.retry_deadline_at is
    'Automatic recovery cutoff; normally scheduled_at plus 30 minutes.';
comment on column public.threads_publish_queue.last_error_meta is
    'Credential-safe structured Meta publish diagnostics for the latest failure.';
