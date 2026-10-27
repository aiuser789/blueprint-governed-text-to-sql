# Architecture — Governed Text-to-SQL Agent

This document explains how the blueprint is built and why: where each control lives, how identity reaches the database without the model being able to touch it, how row-level security and masking are arranged so a view cannot leak, what the validator is for (and what it is not for), and how to take the pattern to your own warehouse.

---

## 1. Problem framing

A text-to-SQL agent has an unusual security property: **the untrusted component writes the code that touches the data.** The model is steered by the user's question, which can be adversarial ("ignore the region filter"), and by whatever text is in its context. Any control that lives in the prompt is a suggestion to that component.

So the design rule is:

> **Everything that decides access is enforced by PostgreSQL or by code the model cannot change. The model is used for what it is good at — reading a question and writing a query, reading a result and writing a sentence — and for nothing that decides who may see what.**

Three further failure modes shape the design, because they produce wrong answers that *look* right:

- **Empty-looking answers.** A West manager asking for East revenue gets zero rows from RLS. Reported naively, that is "$0 East revenue" — a false fact. The agent must say *why* it is empty.
- **Masked-value filters.** `WHERE full_name LIKE 'J%'` against a masked column silently matches nothing. Harmless for privacy (the database compared against the mask), harmful for correctness. The agent must refuse and say why.
- **Definitions.** "Active customer" has two approved definitions here; "this year" is calendar or fiscal. Picking one silently is a wrong answer half the time. The agent must ask.

## 2. System shape

```
 identity → period → screens → ground → [ plan → validate → execute ] ×≤3 → compose → output scan → scope → audit
   code      code      code      code       MODEL     code       DATABASE          MODEL       code        code    code
```

| Module | Role |
|---|---|
| `agent.py` | The pipeline and the retry loop; assembles the `AskResult` with a stage trace |
| `periods.py` | Resolves relative periods against `AS_OF_DATE`; flags ambiguous ones |
| `screens.py` | Pre-model checks: identity change, write intent, PII above tier, PII filters, unsupported concepts, ambiguity; the scope line and scope note |
| `knowledge.py` | The catalog and glossary, trimmed to the asker's role and annotated for their tier |
| `planner.py` | The `Plan` contract every backend returns; delegates the offline planner to `domains/retail/planner.py` |
| `domains/retail/` | The retail data: catalog, glossary, data generator, offline planner, retail-only screens, example questions, demo |
| `domains/banking/` | The banking reference: synthetic `demo_banking` schema and its reviewed dataset spec |
| `model_backends.py`, `prompts.py` | Anthropic and OpenAI-compatible backends; plan + compose only |
| `validator.py` | Syntax-tree policy check and row cap; re-renders the SQL that will run |
| `executor.py` | Per-question agent connection, session minting, read-only transaction, timeout, revocation |
| `output_scan.py` | Last-line PII pattern scan on prose and cells; fails closed |
| `store.py` | Audit log, export requests, glossary proposals (service role only) |
| `src/sql_agent/migrations/` | Roles' privileges, tables, identity functions, RLS, masked views, audit, approvals |

Every request uses **two database connections with two roles**:

- the **service role** (`DATABASE_URL`) mints the session token, writes the audit row and reads the approval tables. It never runs model-written SQL;
- the **agent role** (`nl2sql_agent`) runs model-written SQL. It can read the `analytics` views and call `ctx.*()` — nothing else.

## 3. Identity: a session token bound to one connection

The obvious way to tell PostgreSQL who is asking is `SET app.role = 'supervisor_officer'` and policies that read `current_setting('app.role')`. That is fragile: the query runs in the same session, so a query that calls `set_config('app.role', 'analyst', true)` changes its own identity. The validator blocks `set_config`, but a control that depends on the validator is not a database control.

Instead (`src/sql_agent/migrations/0002_identity.sql`, `executor.py`):

1. The executor opens a **fresh** agent connection and reads its `pg_backend_pid()`.
2. On the **service** connection it inserts into `private.sessions`: a random 128-bit token, the user id, that PID, and an expiry (`SESSION_TTL_SECONDS`, 60 s by default).
3. On the agent connection: `BEGIN READ ONLY`, `SET LOCAL statement_timeout`, `set_config('app.session_token', token, true)` (transaction-local), the query — sent as a server-side prepared statement, so PostgreSQL itself refuses a second statement — then `ROLLBACK`.
4. The service connection deletes the token (and any expired ones).

Every policy and masking expression asks `ctx.role()`, `ctx.region()`, `ctx.rep_id()`, `ctx.pii_tier()`. Those resolve the token through a `SECURITY DEFINER` function that returns a user **only if the token is live and was minted for this exact backend PID**. Consequences:

- A query that overwrites `app.session_token` needs another user's live token — it cannot read `private.sessions` to find one, and cannot insert one.
- A token copied to another connection is useless (wrong PID). `test_token_is_bound_to_its_connection` proves it.
- No session, an expired token or a guessed token resolves to no user, and every policy then returns no rows (`test_no_session_means_no_rows`).
- A fresh connection per question means no setting, prepared statement or temp object can carry from one user to the next.

`ctx.*()` only ever reveal the caller's *own* identity, so granting `EXECUTE` on them to the agent role exposes nothing.

## 4. Row-level security

`src/sql_agent/migrations/0003_rls.sql`. Policies are `FOR SELECT` on the base tables in `core`:

| Role | customers | orders / order_items | sales_reps | summaries |
|---|---|---|---|---|
| analyst, privacy_officer | all | all | all | all |
| supervisor_officer | `region = ctx.region()` | `orders.region = ctx.region()` | their region | their region |
| sales_rep | `assigned_rep_id = ctx.rep_id()` | orders of those customers | themselves | none |
| contractor | none | none | none | all (minimum group size) |
| no valid session | none | none | none | none |

Two details matter more than the predicates:

- **`FORCE ROW LEVEL SECURITY`.** The analytics views are ordinary (owner-privilege) views, so base tables are read with the *view owner's* privileges. Without `FORCE`, the owner is exempt from RLS and every view would leak. With it, the owner (`nl2sql_owner`) is filtered like anyone else.
- **The owner is not a superuser.** A superuser bypasses RLS unconditionally, `FORCE` or not. That is why migrations create `nl2sql_owner` (NOLOGIN, NOBYPASSRLS) and hand it ownership of every table and view, even though a superuser runs the migrations.

`ctx.*()` calls are wrapped in scalar sub-selects (`(SELECT ctx.role())`) so PostgreSQL evaluates them once per query as an InitPlan rather than once per row; a 50,000-row scan stays in the tens of milliseconds.

Which `region` is used is a business decision made explicit: orders carry the region **at the time of the order**, customers their **current** region. Forty synthetic customers moved region in July 2025, so a West manager sees West orders, including some placed by customers who are now in the East, and does not see their customers' records. Change the policy if your business wants the other reading; do not leave it implicit.

## 5. PII masking

`src/sql_agent/migrations/0004_masked_views.sql`. The masking is a `CASE (SELECT ctx.pii_tier()) …` per PII column, **inside the view**, so masking happens before a value leaves the database:

| column | full | partial | masked |
|---|---|---|---|
| full_name | Jane Doe | Jane D. | Customer #4821 |
| email | jane.doe@example.com | j***@example.com | [REDACTED] |
| phone | +1-206-555-0182 | \*\*\*-\*\*\*-0182 | [REDACTED] |
| street_address | 12 Oak St | [REDACTED] | [REDACTED] |
| city | Charlotte | Charlotte | [REDACTED] |
| postal_code | 28202 | 282\*\* | [REDACTED] |
| date_of_birth | 1984-03-09 | 1984 | [REDACTED] |

Properties that follow:

- **The model never sees raw values it is not entitled to.** It composes the answer from the masked rows.
- **Filters can't probe.** Because the masked value is the view's output column, `WHERE full_name LIKE 'A%'` is evaluated against `Customer #…`. The validator rejects such filters for correctness (§6), but the database would not leak even if it didn't (`test_filters_run_against_masked_values`).
- **`security_barrier`** stops the planner from pushing a caller's non-leakproof function below the view's own row filter, the classic way to make a function observe rows a policy would have removed.
- **Derived, non-PII columns replace PII for analysis.** `age_band` lets an analyst ask about ages without `date_of_birth`.
- **Employee PII is PII.** Rep names and emails are masked the same way.

The contractor role reads `region_month_summary` and `category_month_summary`, pre-aggregated by the service role (`private.refresh_aggregates()`), with groups under 10 orders/customers/lines suppressed both at build time and in the view.

## 6. The validator

`validator.py`. sqlglot parses the SQL (PostgreSQL dialect); rules depend on who is asking:

| Rule | Rejects |
|---|---|
| `parse_error` | Anything unparseable |
| `multiple_statements` | `SELECT 1; DROP …`, including comment- and dollar-quote-hidden ones |
| `not_select` | DML, DDL, `SET`, `COPY`, transactions, `SELECT INTO`, `FOR UPDATE`, `EXPLAIN`, data-modifying CTEs |
| `schema_not_allowed` | `core.`, `private.`, `pg_catalog.`, `information_schema.` or any non-analytics schema |
| `table_not_allowed` | A view the asker's role may not query, or a name that isn't one of their views |
| `function_not_allowed` | Anything outside an allowlist (aggregates, windows, arithmetic, dates, a few string functions). `set_config`, `current_setting`, `pg_sleep`, `pg_read_file`, `dblink`, `generate_series` … are out |
| `select_star` | `*` outside `COUNT(*)` |
| `pii_in_filter` | A PII column the tier cannot see used in WHERE, JOIN ON, HAVING, ORDER BY or PARTITION BY |
| `pii_distinct_count` | `COUNT(DISTINCT <pii>)` below full tier |

Selecting or grouping by a masked column is allowed: the database masks it, and grouping by `Customer #3284` is grouping by the customer.

What runs is the validator's **re-rendered tree** with comments stripped (`tree.sql(dialect='postgres', comments=False)`), not the model's text. If sqlglot and PostgreSQL ever read a string differently, PostgreSQL still only receives what was checked. The row cap is applied to the tree (`LIMIT max_rows + 1`, or the query's own smaller limit), so truncation is detected and reported.

The validator is **not** the security boundary, and the evals treat it that way: the red-team pack runs every hostile statement a second time with the validator bypassed. Its job is to turn bad queries into precise feedback the model can fix, keep pointless load off the database, and stop correctness traps (masked filters) that the database would happily execute.

## 7. The model boundary

What the model is given: the role-trimmed catalog (views, columns, types, descriptions, and for each PII column what *this* tier sees), the approved glossary, the resolved period, the as-of date, and the question. It is not told that base tables, sessions, users or the audit log exist. A contractor's model has never heard of the `customers` view.

What it returns: one JSON object — `sql`, `clarify` or `decline`. Malformed replies are treated as no SQL and retried with feedback; three failures end in an honest decline. Provider errors end the question with "nothing was run", never with a guess.

What it can't do, whatever it writes: change identity, reach another schema, write, outlive the timeout, see unmasked PII, or put PII into the answer (the output scan replaces a prose answer that contains email, phone, street-address or date-of-birth patterns with a templated one). `tests/test_agent_pipeline.py::HostileModel` is a backend that writes attacks and then invents contact details; it gets a decline, then a scoped, masked table with its prose replaced.

**Which model.** The recommended profile is a local model served by **Ollama** (`MODEL_BACKEND=ollama`, `OllamaBackend`): the prompt carries the role-trimmed schema and glossary, and the answer step carries masked result rows, so with a local model none of that leaves the machine, and there is no per-question cost or API key. The backend calls Ollama's `/api/chat` with the standard library, requests plans with `format: "json"` and temperature 0, and always sends `num_ctx` (8192 by default), because Ollama's default context window is smaller than the prompt and a truncated prompt fails silently. Hosted models (Anthropic, OpenAI-compatible) plug into the same two methods and generally write better SQL for hard questions. The choice moves answer quality, not access: every control above is enforced after the model has finished.

## 8. Human gates and audit

**Exports.** A result beyond `MAX_RESULT_ROWS` is truncated and flagged. The user can request an export with a reason; the request stores the *validated* SQL. An approver (`can_approve`) who is not the requester decides with a reason (`CHECK (decided_by <> requested_by)` backs the code check). On download, the stored SQL is validated again and run **as the requester** — their RLS scope and PII tier — up to `EXPORT_MAX_ROWS`. Approval grants volume, never access.

**Glossary proposals.** A new definition is proposed with its SQL hint and approved by a different approver before it joins the glossary a real model is grounded on.

**Audit.** One row per question in `private.audit_log`: user, role, region and tier at the time; the question; `kind` and `reason_code`; the SQL; attempts; the validator rules that fired; row count and truncation; the scope line; the backend; duration; the output-scan result. Result values are not stored. The privacy officer reads everyone's rows via `/api/audit`; everyone else reads their own.

## 9. Deploying on your PostgreSQL, adapting to your data

**Migrator privileges.** `sql-agent migrate` does not need a superuser. It needs `CREATEROLE` (to create `nl2sql_owner` and `nl2sql_agent`) and `CREATE` on the database (for its schemas) — what Supabase, RDS, Cloud SQL and Azure give their admin user. It grants itself membership in `nl2sql_owner` (to hand that role the tables) and refuses to continue if either blueprint role has `SUPERUSER` or `BYPASSRLS`. Loading the dataset and rebuilding the summaries happen in a maintenance transaction: the migrator switches to the owner role, lifts `FORCE ROW LEVEL SECURITY`, does the work and re-forces it before committing. `ALTER TABLE` holds an exclusive lock until commit and DDL is transactional, so no other session ever reads a table in the lifted state, and a failure rolls it back. Outside that transaction a non-superuser migrator sees zero rows in `core.*` like everyone else. On Supabase, use the session pooler (each question's token is bound to one backend) and a strong `AGENT_DB_PASSWORD`; the agent's pooler login `nl2sql_agent.<project-ref>` is derived from `DATABASE_URL`.

**Your own warehouse.** The pattern carries; the specifics are yours:

1. Classify columns: which are PII, which are quasi-identifiers (city, postal code, birth date), which are safe.
2. Write `core` (or keep your existing schemas) owned by a dedicated NOLOGIN, NOBYPASSRLS role, with `FORCE ROW LEVEL SECURITY` and policies on `ctx.*()`.
3. Map your identity provider's claims to `private.app_users` (role, scope keys, PII tier). Replace `current_user` in `api.py` with JWT / SSO verification.
4. Expose one `security_barrier` view per business entity in `analytics`, with masking `CASE`s per tier and derived non-PII columns for common analysis.
5. Describe those views in `catalog.json`, your definitions in `glossary.json`, and your role → view visibility in each view's `roles`.
6. Rewrite the golden set and the red-team pack for your schema before you trust it. The comparator and leak checker are schema-agnostic.

**Bring your own schema, automated.** `sql-agent dataset propose <schema>` writes the spec for steps 1, 4 and 5 (PII guesses from column names, descriptions from comments, join keys from foreign keys, values of small categorical columns); `sql-agent dataset apply <schema>` builds the `analytics.<schema>_<table>` views from it. Rather than altering your tables (RLS on them, new owners), each view carries the row rule in its own `security_barrier` WHERE clause and the masking in its select list, evaluated with the same `ctx.*()` session identity; it is owned by `nl2sql_owner`, which is granted SELECT on the source tables, and only the view is granted to the agent role. For source tables that already have RLS (Supabase dashboard tables), a `FOR SELECT TO nl2sql_owner USING (true)` policy lets the view read them; the view's own WHERE clause is then the row control. `RETAIL_DATA=off` removes the retail data from the catalog, glossary and screens.

**Other warehouses.** Snowflake (row access policies + masking policies + secondary roles), BigQuery (row-level access policies + policy tags / data masking) and Databricks (row filters + column masks) have native equivalents of §4 and §5. The executor's job becomes "run as the asker" through the warehouse's own identity mechanism; the validator, screens and evals carry over.

## 10. Production hardening

- **Identity.** Replace `X-Demo-User` with verified SSO / JWT; set `DEMO_IDENTITY=off`. Keep identity out of request bodies.
- **Secrets.** `AGENT_DB_PASSWORD` and the service credentials from a secret store; rotate (the migration resets the agent password on every run).
- **Network.** The agent role should be able to connect only from the API's network; consider a separate database user per deployment.
- **Rate and cost limits.** Per-user question rate, per-question token budget, and `statement_timeout` / `work_mem` tuned to your warehouse.
- **Replica.** Point the agent role at a read replica; nothing it does needs the primary.
- **Audit retention and export.** Ship `private.audit_log` to your SIEM; treat question text as potentially sensitive.
- **Aggregate inference.** Minimum group sizes reduce but do not prevent differencing attacks; for external roles consider noise (differential privacy) or a query-auditing budget.
- **Catalog metadata.** `pg_catalog` is readable by every PostgreSQL role (policy definitions, view SQL). The validator blocks it; if your policy logic is itself sensitive, run the agent against a database where it isn't.

## 11. Threat model

| Attack | First line | Holds even if that fails |
|---|---|---|
| "Ignore your rules", "pretend I'm Lena", "as user lena" | `screens.screen_identity` | Identity is the session token; the question never reaches identity |
| `set_config('app.session_token', …)` / `SET ROLE` | validator (`function_not_allowed`, `not_select`) | Token is PID-bound and unguessable; `SET ROLE nl2sql_owner` is denied (no membership) |
| Read `core.*`, `private.*`, `pg_shadow` | validator (`schema_not_allowed`) | No `USAGE` on those schemas; `pg_shadow` is superuser-only |
| Write: DML, DDL, data-modifying CTE, `SELECT INTO`, `COPY` | validator (`not_select`) | SELECT-only grants; read-only transaction; no TEMP |
| Stacked statements, comment tricks | validator (`multiple_statements`) | The SQL is sent as a server-side prepared statement, which PostgreSQL refuses to hold more than one command in; read-only anyway |
| Legal SQL that over-reaches ("all regions") | — (it is legal) | RLS returns only the asker's rows; the scope note says so |
| Probe PII by filter / sort / distinct count | validator (`pii_in_filter`, `pii_distinct_count`) | The filter runs on masked values |
| Model invents PII in its prose | output scan (fails closed) | The model never saw raw PII to begin with |
| Denial of service (`pg_sleep`, cartesian joins) | function allowlist | `statement_timeout`, row cap |
| Contractor asks for rows | role-trimmed catalog, validator | RLS returns nothing for contractor on row tables |
| Self-approved export | `store.decide_export` | `CHECK (decided_by <> requested_by)`; export runs as the requester |

## 12. Evals

`evals/run_evals.py` is the gate (`make evals`). It runs:

- **golden.jsonl** (70 cases, built by `build_golden.py`): behaviour (kind + reason), execution accuracy against a reference query **run as the same user**, and scope notes. The comparator matches result columns by values (ordered or as multisets), so a model's query with extra columns or different aliases still passes when the numbers are right.
- **redteam.jsonl** (28 statements): validator outcome, then the same SQL straight into the database with the validator bypassed; the database must deny, time out, or return only in-scope masked rows.
- **Leak, scope and integrity checks** over every output: cells and prose compared against the real PII values (read with the service role); region / book / contractor scope; row counts and checksums of every base table before and after.

The gate passes only with zero leaks, zero scope violations, an unchanged database, every red-team case as expected, and ≥ 95% behaviour and execution accuracy. `evals/run_llm_evals.py` runs the golden set through a real model; security figures must still be zero there, because they do not depend on the model.

## 13. Fitting it into TridaPad / AgentSQL

The agent is a service with one seam: `POST /api/ask` with an identity header and a question, returning SQL, rows, a scope line and an audit id (`examples/api_client.py`). An AgentSQL-style chat surface or a TridaPad MCP tool can call it as-is; the important part is that the *caller's* authenticated user is what reaches `current_user`, and that saving an answer as a shared query or dashboard re-runs it under each **viewer's** identity rather than freezing the author's result for everyone.
