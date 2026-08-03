-- Manual migration — this repo has no migration tooling, so run this
-- directly against the Supabase project's SQL editor (or `psql`) once,
-- before deploying persist_worker.py.
--
-- Adds the idempotency key persist_worker.py upserts on. Pub/Sub only
-- guarantees at-least-once delivery, so a message can be redelivered after
-- a worker crash, a slow ack, etc — this UNIQUE constraint is what makes
-- persist_worker's `upsert(..., on_conflict="client_msg_id",
-- ignore_duplicates=True)` actually prevent duplicate rows instead of just
-- hoping redelivery doesn't happen.
--
-- Nullable because pre-existing rows (written before this pipeline existed,
-- via the old direct-insert send_message()) never had a client_msg_id and
-- shouldn't be backfilled with a fake one.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS client_msg_id uuid UNIQUE;
