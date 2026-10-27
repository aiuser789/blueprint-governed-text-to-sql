"""The banking reference (synthetic).

- ``demo_banking_schema.sql``        a synthetic ``demo_banking`` schema (branches, customers,
                                     accounts, transactions, loans) to try onboarding safely
- ``demo_banking.example.json``      the reviewed dataset spec for it: PII kinds, per-role row
                                     rules, notes and example questions

Banking data is served through the shared dataset engine (``sql_agent.datasets``):
``sql-agent dataset propose <schema>`` writes a spec to DATASETS_DIR, you review it,
``sql-agent dataset apply <schema>`` creates the governed views.
"""
