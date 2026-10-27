# Security

## Prototype — not hardened for exposure

This blueprint is a **reference prototype**. The `X-Demo-User` header that picks a user in the console is a demo switch, not authentication, and optional `API_KEY` auth is a shared secret for local demos, not an identity system.

- Run it locally or inside a network you control.
- **Do not expose it to the public internet** as-is. A production deployment needs verified identity (SSO / JWT mapped to `private.app_users`), rate limits, secret management and the rest of `docs/architecture.md` §10.

## What the design does guarantee

The access controls themselves are not prototype-grade: rows are filtered by PostgreSQL row-level security, PII is masked inside the database views, and model-written SQL runs as a SELECT-only role in a read-only transaction with a timeout, under a per-question identity token bound to its own connection. The red-team eval pack runs hostile SQL with the validator bypassed to check that these hold on their own. If you find a way around any of them, that is a security issue — please report it.

## Data

- All bundled data is synthetic. Never load real customer, employee or order data into this prototype, and never include real data in issues, pull requests, logs or screenshots.
- The audit log stores question text. A user who types PII into a question puts it in the log (readable by the privacy officer role only).
- The code reads optional model API keys from environment variables and loads `.env` from the repo root at startup (real environment variables take precedence). `.env` is git-ignored — never commit a filled-in one.

## Reporting a concern

If you find a security concern in this repository, please report it privately through Trida AI's contact route at https://trida.ai/intake rather than opening a public issue.
