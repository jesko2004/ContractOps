DROP TABLE IF EXISTS event_dead_letters CASCADE;
DROP TABLE IF EXISTS notification_deliveries CASCADE;

DROP INDEX IF EXISTS outbox_publish_claim_idx;
ALTER TABLE outbox_events
    DROP CONSTRAINT IF EXISTS outbox_events_tenant_id_id_key,
    DROP COLUMN IF EXISTS dead_lettered_at,
    DROP COLUMN IF EXISTS last_error,
    DROP COLUMN IF EXISTS stream_message_id,
    DROP COLUMN IF EXISTS locked_until,
    DROP COLUMN IF EXISTS locked_by,
    DROP COLUMN IF EXISTS next_attempt_at,
    DROP COLUMN IF EXISTS max_attempts;

CREATE INDEX outbox_unpublished_idx ON outbox_events (occurred_at)
    WHERE published_at IS NULL;
