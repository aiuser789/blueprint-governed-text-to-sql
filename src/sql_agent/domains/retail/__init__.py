"""The retail data (synthetic): 16 reps, 60 products, 5,000 customers, 50,000 orders.

- ``catalog.json``   the analytics views the model is told about, and what each PII tier sees
- ``glossary.json``  approved business definitions, ambiguous terms, unanswerable concepts
- ``seed.py``        the deterministic data generator (loaded by ``sql-agent migrate``)
- ``warehouse.py``   the core.* tables and the monthly summaries built from them
- ``planner.py``     the offline planner behind MODEL_BACKEND=mock
- ``rules.py``       retail-only question screens (missing period, cities, unsupported)
- ``examples.json``  example questions shown in the console
- ``demo.py``        the ``sql-agent-demo`` trace

Its tables are created by the numbered migrations in ``src/sql_agent/migrations``.
"""
