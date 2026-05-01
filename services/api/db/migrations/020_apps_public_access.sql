-- migrate:up

ALTER TABLE apps
    ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;

-- migrate:down

ALTER TABLE apps
    DROP COLUMN IF EXISTS is_public;
