-- 0006 audit — one row per question, written by the service role.
--
-- What is stored: who asked (user, role, region, tier at the time), the
-- question, what the agent decided and why, the SQL it ran, how many
-- attempts it took, which validator rules fired, the row count and the
-- scope line. What is NOT stored: result values. The log is reviewable
-- by a privacy officer without itself becoming a copy of the data.

CREATE TABLE IF NOT EXISTS private.audit_log (
    audit_id        bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    user_id         text NOT NULL,
    role            text NOT NULL,
    region          text,
    pii_tier        text NOT NULL,
    question        text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('answer', 'clarify', 'decline', 'error', 'export')),
    reason_code     text,
    reason          text,
    sql             text,
    attempts        integer NOT NULL DEFAULT 0,
    rules_fired     jsonb NOT NULL DEFAULT '[]'::jsonb,
    row_count       integer,
    truncated       boolean NOT NULL DEFAULT false,
    scope           text,
    backend         text NOT NULL,
    duration_ms     integer NOT NULL DEFAULT 0,
    output_scan     text NOT NULL DEFAULT 'not_run'
);

CREATE INDEX IF NOT EXISTS audit_log_user_idx ON private.audit_log (user_id, at DESC);
