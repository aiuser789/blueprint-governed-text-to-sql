# Examples

- `api_client.py` — ask the same question as three users over HTTP and print what each gets back. This is the seam a BI tool, chat bot or a TridaPad / AgentSQL integration would call: identity in a header set by the caller's auth layer, the question in the body, the governed answer (SQL, rows, scope line, audit id) back.

Bringing your own warehouse is a different kind of change, and it happens in the database, not here: see "Adapting it to your data" in `docs/architecture.md`.

The banking reference (a synthetic `demo_banking` schema and its reviewed dataset spec) now lives in `src/sql_agent/domains/banking/`, and the retail data in `src/sql_agent/domains/retail/`.
