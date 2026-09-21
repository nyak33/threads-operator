-- Heartbeat checkpoint for the deterministic publish queue.
--
-- Why: a row claimed by a worker (status='posting') that crashes mid-thread
-- stayed invisible until a human noticed. With a per-step heartbeat timestamp
-- the watchdog can detect stale claims deterministically:
--   status='posting' AND heartbeat_at older than the stale window (default
--   10 minutes) -> safely reclaimable: another worker re-claims via a CAS
--   PATCH (claimed_at filter) and RESUMES the thread from the persisted
--   checkpoint (threads_main_post_id + threads_reply_ids are saved after
--   every successful publish step, so resume never recreates the root and
--   reconciles live before re-publishing any reply slot).
--
-- Additive and default-safe: existing rows get heartbeat_at=NULL. Readers
-- must treat NULL as "fall back to claimed_at/updated_at" until this
-- migration is applied (the store code does this automatically, so the
-- worker is fully functional before and after applying it).

alter table public.threads_publish_queue
    add column if not exists heartbeat_at timestamptz;

-- Index for the watchdog scan (small table; keeps the query index-friendly
-- as history grows).
create index if not exists idx_publish_queue_posting_heartbeat
    on public.threads_publish_queue (status, heartbeat_at)
    where status = 'posting';
