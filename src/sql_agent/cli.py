"""Command line.

    sql-agent migrate                         apply migrations + load the dataset
    sql-agent ask --user priya "Revenue last month?"
    sql-agent ask --user sam --json "Top 5 customers by revenue in Q2 2026"
    sql-agent compare "Top 5 customers by revenue last quarter"
    sql-agent users
    sql-agent audit [--limit 20]
    sql-agent viewer                          check the database and the model backend
    sql-agent dataset propose banking         describe your own schema → datasets/banking.json
    sql-agent dataset apply banking           create the governed views from that file
    sql-agent dataset list                    what the agent can query, per dataset
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import get_settings


def _print_result(r, as_json: bool) -> None:
    if as_json:
        print(r.model_dump_json(indent=2))
        return
    print(f"\n[{r.user.user_id} · {r.user.role}{' · ' + r.user.region if r.user.region else ''} · PII {r.user.pii_tier}]  {r.question}")
    print(f"  {r.kind.upper()}" + (f" ({r.reason_code})" if r.reason_code else "") + f": {r.message}")
    for o in r.options:
        print(f"    - {o.label}" + (f"  → try: {o.rewrite}" if o.rewrite else ""))
    print(f"  scope: {r.scope}" + (f"\n  note:  {r.scope_note}" if r.scope_note else ""))
    if r.sql:
        print("  sql:\n    " + r.sql.replace("\n", "\n    "))
    if r.rows:
        widths = [max(len(str(c)), *(len(str(row[i])) for row in r.rows[:10])) for i, c in enumerate(r.columns)]
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(r.columns, widths)))
        for row in r.rows[:10]:
            print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
        if r.row_count > 10:
            print(f"  … {r.row_count - 10} more rows" + (" (truncated at the row cap)" if r.truncated else ""))
    for h in r.rules_fired:
        print(f"  rule fired (attempt {h.attempt}): {h.rule} — {h.message}")
    print(f"  stages: " + " → ".join(f"{s.name}:{s.status}" for s in r.stages))
    print(f"  backend: {r.backend} · output scan: {r.output_scan} · audit #{r.audit_id} · {r.duration_ms} ms")


def _viewer(settings) -> int:
    from .executor import Executor
    from .model_backends import build_backend, check_backend

    import os

    ok = True
    print(f"MODEL_BACKEND = {settings.model_backend}")
    if os.environ.get("SAMPLE_DATA") is not None and os.environ.get("RETAIL_DATA") is None:
        print("[warn] SAMPLE_DATA is no longer read: rename it to RETAIL_DATA in .env (same values: on / off)")
    try:
        info = Executor(settings).ping()
        print(f"[ok]   database: PostgreSQL {info['server_version'].split()[0]}, {len(info['migrations'])} migrations, "
              f"~{info['customers']:,} customers, agent role '{info['agent_role']}'")
    except Exception as e:
        ok = False
        print(f"[FAIL] database: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
        print("       Set DATABASE_URL in .env and run: sql-agent migrate")
    try:
        backend = build_backend(settings)
        check = check_backend(backend)
        print(f"{'[ok]  ' if check['ok'] else '[FAIL]'} model: {backend.name} — {check['detail']}")
        ok = ok and check["ok"]
    except Exception as e:
        ok = False
        print(f"[FAIL] model: {e}")
    return 0 if ok else 1


def _dataset(settings, args) -> int:
    import psycopg

    from . import datasets

    if args.action == "list":
        specs, views = datasets.load_all()
        if not views:
            print(f"No datasets onboarded yet (looked in {datasets.datasets_dir()}/). Start with: sql-agent dataset propose <schema>")
        for v in views:
            pii = [c for c, m in v.columns.items() if m.get("pii")]
            print(f"{v.view:32} roles: {', '.join(v.roles) or 'nobody'}  ·  {len(v.columns)} columns, PII: {', '.join(pii) or 'none'}")
        return 0
    if not args.schema:
        print("Name the schema, e.g.: sql-agent dataset propose banking", file=sys.stderr)
        return 2
    if not settings.database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=15) as conn:
        if args.action == "propose":
            target = datasets.datasets_dir() / f"{args.schema}.json"
            if target.exists() and not args.force:
                print(f"{target} already exists — edit it and run `sql-agent dataset apply {args.schema}`, or re-run with --force.",
                      file=sys.stderr)
                return 1
            spec = datasets.propose(conn, args.schema, sample_values=not args.no_samples)
            path = datasets.write_spec(spec, target)
            print(f"Wrote {path} — {len(spec['tables'])} tables.")
            for t in spec["tables"]:
                pii = [f"{c} ({m['pii']})" for c, m in t["columns"].items() if m.get("pii")]
                rls = "  [has RLS]" if t["source_has_rls"] else ""
                print(f"  {t['table']:28} → analytics.{t['view']}{rls}\n      PII guessed: {', '.join(pii) or 'none'}")
            print("\nReview it before applying: PII guesses come from column names only, and only analysts and the privacy\n"
                  f"officer can see rows until you change 'rows'. Then: sql-agent dataset apply {args.schema}")
            return 0
        spec = datasets.load_spec(args.schema)
        views = datasets.apply(conn, spec)
        print(f"Applied {len(views)} governed views: {', '.join('analytics.' + v for v in views)}")
        print("Ask about them now, e.g.: sql-agent ask --user sam \"How many accounts are there by type?\"")
        return 0


def _reapply_datasets(settings) -> int:
    import psycopg

    from . import datasets

    specs = datasets.load_all()[0]
    if not specs:
        return 0
    failed = 0
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for spec in specs:
            try:
                views = datasets.apply(conn, spec)
                print(f"datasets re-applied: {spec['schema']} ({len(views)} views)")
            except Exception as e:  # report and carry on: one bad spec must not block the others
                failed += 1
                print(f"[FAIL] dataset {spec['schema']}: {type(e).__name__}: {str(e).splitlines()[0][:200]}"
                      f" — fix datasets/{spec['schema']}.json, then: sql-agent dataset apply {spec['schema']}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sql-agent", description="Governed text-to-SQL agent (Trida AI blueprint)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply migrations and load the synthetic retail data")
    m.add_argument("--no-data", action="store_true")
    a = sub.add_parser("ask", help="ask a question as a user")
    a.add_argument("--user", required=True)
    a.add_argument("--json", action="store_true")
    a.add_argument("question")
    c = sub.add_parser("compare", help="ask the same question as every demo user")
    c.add_argument("question")
    sub.add_parser("users", help="list the demo users")
    sub.add_parser("viewer", help="check the database and the model backend")
    ds = sub.add_parser("dataset", help="bring your own schema: propose / apply / list")
    ds.add_argument("action", choices=["propose", "apply", "list"])
    ds.add_argument("schema", nargs="?", help="schema name (propose/apply) or a path to a spec file (apply)")
    ds.add_argument("--no-samples", action="store_true", help="propose: do not list values of small categorical columns")
    ds.add_argument("--force", action="store_true", help="propose: overwrite an existing spec file")
    au = sub.add_parser("audit", help="show recent audit rows")
    au.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.cmd == "migrate":
        from .migrations import migrate

        out = migrate(settings, load_data=not args.no_data)
        print(f"migrations applied: {', '.join(out['applied']) or 'none (up to date)'}; retail data loaded: {out['retail_loaded']}")
        if out["applied"]:  # a migration can change roles or rules the dataset views are built on: rebuild them
            return _reapply_datasets(settings)
        return 0

    if args.cmd == "viewer":
        return _viewer(settings)
    if args.cmd == "dataset":
        return _dataset(settings, args)

    from .agent import Agent, UnknownUser

    agent = Agent(settings)
    if args.cmd == "users":
        for u in agent.users().values():
            print(f"{u.user_id:6} {u.role:18} {u.region or '-':6} PII {u.pii_tier:8} {u.about or ''}")
        return 0
    if args.cmd == "audit":
        for row in agent.store.audit(limit=args.limit):
            print(json.dumps(row, default=str))
        return 0
    if args.cmd == "ask":
        try:
            user = agent.user(args.user)
        except UnknownUser:
            print(f"unknown user {args.user!r}; run `sql-agent users`", file=sys.stderr)
            return 2
        _print_result(agent.ask(user, args.question), args.json)
        return 0
    if args.cmd == "compare":
        for user in agent.users().values():
            _print_result(agent.ask(user, args.question), False)
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
