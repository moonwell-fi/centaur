-- migrate:up

CREATE TABLE IF NOT EXISTS thread_traces (
    thread_key  TEXT PRIMARY KEY,
    trace_id    UUID NOT NULL DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE sandbox_sessions ADD COLUMN IF NOT EXISTS trace_id UUID;

UPDATE sandbox_sessions
SET trace_id = gen_random_uuid()
WHERE trace_id IS NULL;

INSERT INTO thread_traces (thread_key, trace_id)
SELECT thread_key, trace_id
FROM sandbox_sessions
WHERE trace_id IS NOT NULL
ON CONFLICT (thread_key) DO NOTHING;

ALTER TABLE sandbox_sessions
    ALTER COLUMN trace_id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN trace_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_traces_trace_id
    ON thread_traces (trace_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sandbox_sessions_trace_id
    ON sandbox_sessions (trace_id);

-- migrate:down

DROP INDEX IF EXISTS idx_sandbox_sessions_trace_id;
DROP INDEX IF EXISTS idx_thread_traces_trace_id;
ALTER TABLE sandbox_sessions DROP COLUMN IF EXISTS trace_id;
DROP TABLE IF EXISTS thread_traces;
