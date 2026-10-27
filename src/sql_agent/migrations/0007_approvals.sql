-- 0007 approvals — the two human gates.
--
--   export_requests     a result larger than MAX_RESULT_ROWS, or an explicit
--                       export, waits for an approver who is NOT the
--                       requester. An approved export re-runs the stored,
--                       already-validated SQL under the REQUESTER's identity
--                       (their RLS scope and PII tier), never the approver's.
--   glossary_proposals  a new metric definition the agent drafted (or a
--                       user proposed) waits for an approver before it
--                       joins the glossary the planner grounds on.

CREATE TABLE IF NOT EXISTS private.export_requests (
    request_id       text PRIMARY KEY,
    requested_by     text NOT NULL REFERENCES private.app_users (user_id),
    question         text NOT NULL,
    sql              text NOT NULL,
    reason           text NOT NULL,
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by       text REFERENCES private.app_users (user_id),
    decision_reason  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    CHECK (decided_by IS NULL OR decided_by <> requested_by)
);

CREATE TABLE IF NOT EXISTS private.glossary_proposals (
    proposal_id      text PRIMARY KEY,
    term             text NOT NULL,
    definition       text NOT NULL,
    sql_hint         text NOT NULL,
    proposed_by      text NOT NULL REFERENCES private.app_users (user_id),
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by       text REFERENCES private.app_users (user_id),
    decision_reason  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    CHECK (decided_by IS NULL OR decided_by <> proposed_by)
);
