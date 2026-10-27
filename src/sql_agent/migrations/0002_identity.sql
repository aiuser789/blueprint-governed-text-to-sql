-- 0002 identity — who is asking, decided outside the model.
--
-- The executor never passes role/region to the database as plain
-- settings a query could overwrite. Instead:
--
--   1. The SERVICE role inserts a row into private.sessions: a random
--      128-bit token, the user id, the agent connection's backend PID,
--      and a short expiry.
--   2. The AGENT connection sets app.session_token = <token> inside its
--      read-only transaction, then runs the model-written SQL.
--   3. Every RLS policy and masking expression asks ctx.*() who is
--      asking. ctx.*() resolve the token -> user ONLY when the token is
--      live AND was minted for this exact backend PID.
--
-- So a hostile query that calls set_config('app.session_token', ...)
-- would need another user's live token bound to its own PID — which
-- the agent role cannot mint, because it cannot write to private.*.
-- The service role deletes the token when the question finishes.

CREATE TABLE IF NOT EXISTS private.app_users (
    user_id       text PRIMARY KEY,
    display_name  text NOT NULL,
    role          text NOT NULL CHECK (role IN ('regional_manager', 'sales_rep', 'analyst', 'privacy_officer', 'contractor')),
    region        text,
    rep_id        text,
    pii_tier      text NOT NULL CHECK (pii_tier IN ('full', 'partial', 'masked')),
    can_approve   boolean NOT NULL DEFAULT false,
    CHECK (role <> 'regional_manager' OR region IS NOT NULL),
    CHECK (role <> 'sales_rep' OR rep_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS private.sessions (
    token        text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES private.app_users (user_id),
    backend_pid  integer NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);

-- ctx.* — the identity of the current question. SECURITY DEFINER so they
-- can read private.* on behalf of a caller that cannot; each returns NULL
-- (and every policy then denies) when there is no valid session.
CREATE OR REPLACE FUNCTION ctx.current_user_row()
RETURNS private.app_users
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = private, pg_temp
AS $$
    SELECT u.*
    FROM private.sessions s
    JOIN private.app_users u USING (user_id)
    WHERE s.token = current_setting('app.session_token', true)
      AND s.backend_pid = pg_backend_pid()
      AND s.expires_at > now()
$$;

CREATE OR REPLACE FUNCTION ctx.user_id() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT (ctx.current_user_row()).user_id $$;

CREATE OR REPLACE FUNCTION ctx.role() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT (ctx.current_user_row()).role $$;

CREATE OR REPLACE FUNCTION ctx.region() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT (ctx.current_user_row()).region $$;

CREATE OR REPLACE FUNCTION ctx.rep_id() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT (ctx.current_user_row()).rep_id $$;

CREATE OR REPLACE FUNCTION ctx.pii_tier() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT coalesce((ctx.current_user_row()).pii_tier, 'masked') $$;
