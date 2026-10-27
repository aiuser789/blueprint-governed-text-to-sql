"""What the agent knows about specific data, kept apart from the shared engine.

- ``retail``   the synthetic retail data: its catalog, glossary, data
               generator, offline planner, question rules and example questions.
- ``banking``  the banking reference: a synthetic demo schema and a reviewed
               dataset spec showing how a banking schema is onboarded.

Everything outside this folder (identity, row-level security, masking, the
validator, the agent, the API) is shared and knows nothing about either domain.
Real banking tables are onboarded with ``sql-agent dataset propose/apply``; their
specs live in DATASETS_DIR (``./datasets`` by default), not in the package.
"""
