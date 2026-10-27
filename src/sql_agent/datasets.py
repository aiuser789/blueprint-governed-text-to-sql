"""Bring your own schema: expose existing tables to the agent, governed.

Two steps, both run by an administrator with the service role:

    sql-agent dataset propose banking     # introspect → datasets/banking.json
    (review and edit datasets/banking.json)
    sql-agent dataset apply banking       # create the governed views

``propose`` reads the schema's tables, columns, primary keys and column
comments, guesses which columns are personal data from their names, lists
the distinct values of small categorical columns (so the model knows that
``account_type`` is 'checking' or 'savings'), and writes an editable JSON
spec. Nothing in the database changes.

``apply`` turns the spec into one view per table in the ``analytics``
schema (``analytics.banking_accounts`` for ``banking.accounts``):

- **Masking** of every column marked ``pii``, by tier, inside the view —
  the same pattern as the retail views. Values never leave the database
  unmasked for a user below full tier.
- **Row rules** per role in the view's WHERE clause, evaluated with the
  same ``ctx.*()`` identity functions as the retail tables. A role with no
  rule sees no rows. The view is a ``security_barrier`` view, so a
  caller's function cannot observe rows the WHERE clause removes.
- The view is owned by ``nl2sql_owner``; the agent role gets SELECT on the
  view only and never any privilege on your schema.

Your tables are not altered. The only changes outside ``analytics`` are
``GRANT USAGE`` on the schema and ``GRANT SELECT`` on the listed tables to
``nl2sql_owner`` (a NOLOGIN role nobody can sign in as), plus — only for
tables that already have row-level security enabled — a read policy for
``nl2sql_owner``, without which your own RLS would hide every row from the
views. Those policies apply to that one role and are dropped if you remove
the table from the spec and apply again.

Row rules are SQL predicates over the table's columns, written by the
administrator in the spec. They can use ``ctx.user_id()``, ``ctx.role()``,
``ctx.region()`` and ``ctx.rep_id()`` (the asker, from the session) — for
example ``"branch_region = ctx.region()"``. ``"all"`` means every row,
``"none"`` (or leaving the role out) means no rows.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import psycopg
from psycopg import sql

from .config import AGENT_ROLE, OWNER_ROLE

ROLES = ("supervisor_officer", "sales_rep", "analyst", "privacy_officer", "contractor")
TIERS = ("full", "partial", "masked")

# What each PII kind shows per tier. "pseudonym" = '<Table> #<primary key>'.
MASK_KINDS: dict[str, dict[str, str]] = {
    "name":       {"full": "full", "partial": "partial", "masked": "pseudonym"},
    "first_name": {"full": "full", "partial": "full", "masked": "redacted"},
    "last_name":  {"full": "full", "partial": "partial", "masked": "redacted"},
    "email":      {"full": "full", "partial": "partial", "masked": "redacted"},
    "phone":      {"full": "full", "partial": "partial", "masked": "redacted"},
    "address":    {"full": "full", "partial": "redacted", "masked": "redacted"},
    "city":       {"full": "full", "partial": "full", "masked": "redacted"},
    "postal":     {"full": "full", "partial": "partial", "masked": "redacted"},
    "dob":        {"full": "full", "partial": "partial", "masked": "redacted"},
    "id_number":  {"full": "full", "partial": "partial", "masked": "redacted"},
    "account":    {"full": "full", "partial": "partial", "masked": "redacted"},
    "redact":     {"full": "full", "partial": "redacted", "masked": "redacted"},
}

_PERSON_TABLE = re.compile(r"customer|client|user|member|person|people|employee|staff|holder|beneficiar|contact|borrower|applicant|party|parties")
_PII_RULES: list[tuple[str, re.Pattern]] = [
    ("account", re.compile(r"(^|_)(account|acct)_?(no|num|number)$|^iban$|(^|_)card_?(no|num|number)$|^pan$|routing|^swift|^bic$|sort_?code")),
    ("id_number", re.compile(r"ssn|social_?security|tax_?id|(^|_)tin$|national_?id|passport|driver|licen[cs]e_?(no|num|number)|^nin$|aadhaar|(^|_)pan_?(no|number)$")),
    ("email", re.compile(r"e_?mail")),
    ("phone", re.compile(r"phone|mobile|(^|_)cell|(^|_)fax|(^|_)tel(ephone)?($|_)")),
    ("dob", re.compile(r"(^|_)dob$|birth")),
    ("first_name", re.compile(r"^(first|given)_?name$")),
    ("last_name", re.compile(r"^(last|family|sur)_?name$")),
    ("name", re.compile(r"^(full_?name|customer_?name|client_?name|holder_?name|account_?holder(_?name)?|beneficiary_?name|"
                        r"employee_?name|contact_?name|borrower_?name|applicant_?name|member_?name|user_?name|payee_?name)$")),
    ("address", re.compile(r"address|street|addr_?line|^addr|house_?(no|number)")),
    ("postal", re.compile(r"zip|postal|post_?code|pin_?code")),
    ("city", re.compile(r"^city$|(^|_)town$")),
]
_SAFE_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "core", "private", "analytics", "ctx"}


def datasets_dir() -> Path:
    return Path(os.environ.get("DATASETS_DIR", "").strip() or "datasets")


# Columns that describe a PII field rather than hold it: phone_type, email_verified, address_status …
_DESCRIPTOR = re.compile(r"(^is_|^has_|_(type|kind|category|flag|status|verified|preference|preferred|count|opt_in|consent|source|label)$)")


def guess_pii(table: str, column: str) -> str | None:
    c = column.lower()
    if _DESCRIPTOR.search(c):
        return None
    for kind, rx in _PII_RULES:
        if rx.search(c):
            return kind
    if c == "name" and _PERSON_TABLE.search(table.lower()):
        return "name"
    return None


def view_name(schema: str, table: str) -> str:
    return f"{schema}_{table}".lower()


# ---------------------------------------------------------------------------- propose
def propose(conn: psycopg.Connection, schema: str, *, sample_values: bool = True) -> dict:
    if schema in _SAFE_SYSTEM_SCHEMAS:
        raise ValueError(f"'{schema}' is a system or blueprint schema.")
    tables = conn.execute(
        "SELECT c.relname, obj_description(c.oid, 'pg_class'), c.reltuples::bigint, c.relrowsecurity"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname", (schema,)).fetchall()
    if not tables:
        raise ValueError(f"No tables found in schema '{schema}' (or this user cannot see them).")
    spec_tables = []
    for table, comment, est_rows, has_rls in tables:
        cols = conn.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), col_description(a.attrelid, a.attnum)"
            " FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            (f'"{schema}"."{table}"',)).fetchall()
        pk = conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = %s::regclass AND i.indisprimary", (f'"{schema}"."{table}"',)).fetchall()
        fks = conn.execute(
            "SELECT a.attname, cf.relname, af.attname FROM pg_constraint k"
            " JOIN pg_class cf ON cf.oid = k.confrelid"
            " JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]"
            " JOIN pg_attribute af ON af.attrelid = k.confrelid AND af.attnum = k.confkey[1]"
            " WHERE k.conrelid = %s::regclass AND k.contype = 'f' AND array_length(k.conkey, 1) = 1",
            (f'"{schema}"."{table}"',)).fetchall()
        fk_map = {col: f"{view_name(schema, ref_table)}.{ref_col}" for col, ref_table, ref_col in fks}
        columns: dict[str, dict] = {}
        for name, typ, col_comment in cols:
            kind = guess_pii(table, name)
            desc = col_comment or ""
            if name in fk_map:
                desc = (desc + " " if desc else "") + f"Joins {fk_map[name]}."
            entry: dict = {"type": typ, "description": desc}
            if kind:
                entry["pii"] = kind
            elif sample_values and (typ == "text" or typ.startswith("character")):
                values = _small_value_set(conn, schema, table, name)
                if values:
                    entry["description"] = (desc + " " if desc else "") + "Values: " + ", ".join(repr(v) for v in values) + "."
            columns[name] = entry
        region_col = next((c for c in columns if re.search(r"region", c, re.I) and "pii" not in columns[c]), None)
        rows = {"analyst": "all", "privacy_officer": "all", "supervisor_officer": "none", "sales_rep": "none", "contractor": "none"}
        if region_col:
            rows["supervisor_officer"] = f"{region_col} = ctx.region()"
        spec_tables.append({
            "table": table,
            "view": view_name(schema, table),
            "description": comment or f"Rows from {schema}.{table} (about {max(est_rows, 0):,} rows).",
            "primary_key": pk[0][0] if len(pk) == 1 else None,
            "source_has_rls": bool(has_rls),
            "columns": columns,
            "rows": rows,
        })
    return {
        "schema": schema,
        "_how_to_edit": (
            "Check every 'pii' (remove it from columns that are not personal data, add it where it is missing; kinds: "
            + ", ".join(MASK_KINDS) + "). Improve descriptions — the model reads them. Set 'rows' per role: 'all', 'none', "
            "or a SQL condition on this table's columns using ctx.user_id(), ctx.role(), ctx.region(), ctx.rep_id(). "
            "Delete tables the agent should not see. Then: sql-agent dataset apply " + schema),
        "notes": "",
        "tables": spec_tables,
    }


def _small_value_set(conn: psycopg.Connection, schema: str, table: str, column: str, limit: int = 12) -> list[str]:
    q = sql.SQL("SELECT DISTINCT {c}::text FROM (SELECT {c} FROM {s}.{t} LIMIT 20000) x WHERE {c} IS NOT NULL LIMIT {n}").format(
        c=sql.Identifier(column), s=sql.Identifier(schema), t=sql.Identifier(table), n=sql.Literal(limit + 1))
    try:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '5s'")
            vals = [r[0] for r in conn.execute(q).fetchall()]
    except psycopg.Error:
        return []
    if not vals or len(vals) > limit or any(len(v) > 40 for v in vals):
        return []
    return sorted(vals)


def write_spec(spec: dict, path: Path | None = None) -> Path:
    path = path or datasets_dir() / f"{spec['schema']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------- apply
def _masked_expr(col: str, kind: str, table: str, pk: str | None) -> sql.Composable:
    c = sql.SQL("{}::text").format(sql.Identifier(col))
    partial = {
        "name": sql.SQL("split_part({c}, ' ', 1) || ' ' || left(split_part({c}, ' ', 2), 1) || '.'").format(c=c),
        "first_name": c,
        "last_name": sql.SQL("left({c}, 1) || '.'").format(c=c),
        "email": sql.SQL("left({c}, 1) || '***@' || split_part({c}, '@', 2)").format(c=c),
        "phone": sql.SQL("'***-***-' || right(regexp_replace({c}, '\\D', '', 'g'), 4)").format(c=c),
        "postal": sql.SQL("left({c}, 3) || '**'").format(c=c),
        "city": c,
        "dob": sql.SQL("left({c}, 4)").format(c=c),
        "id_number": sql.SQL("'****' || right({c}, 4)").format(c=c),
        "account": sql.SQL("'****' || right({c}, 4)").format(c=c),
    }
    vis = MASK_KINDS[kind]
    redacted = sql.Literal("[REDACTED]")

    def for_tier(tier: str) -> sql.Composable:
        v = vis[tier]
        if v == "full":
            return c
        if v == "partial":
            return partial.get(kind, redacted)
        if v == "pseudonym" and pk:
            label = table.rstrip("s").replace("_", " ").title() or "Row"
            return sql.SQL("{} || {}::text").format(sql.Literal(f"{label} #"), sql.Identifier(pk))
        return redacted

    return sql.SQL("CASE (SELECT ctx.pii_tier()) WHEN 'full' THEN {f} WHEN 'partial' THEN {p} ELSE {m} END").format(
        f=for_tier("full"), p=for_tier("partial"), m=for_tier("masked"))


RENAMED_ROLES = {"regional_manager": "supervisor_officer"}  # old key in a spec → current role


def role_rows(rows: dict | None) -> dict:
    """A spec's per-role rules with renamed roles mapped to their current name (old specs keep working)."""
    out = dict(rows or {})
    for old, new in RENAMED_ROLES.items():
        if old in out:
            value = out.pop(old)
            out.setdefault(new, value)
    return out


def _row_filter(rows: dict) -> sql.Composable:
    rows = role_rows(rows)
    parts = []
    for role in ROLES:
        rule = (rows or {}).get(role, "none")
        if rule is None or str(rule).strip().lower() in ("", "none", "false"):
            continue
        pred = sql.SQL("true") if str(rule).strip().lower() in ("all", "true") else sql.SQL(f"({rule})")
        parts.append(sql.SQL("((SELECT ctx.role()) = {r} AND {p})").format(r=sql.Literal(role), p=pred))
    return sql.SQL(" OR ").join(parts) if parts else sql.SQL("false")


def build_view_sql(schema: str, t: dict) -> sql.Composable:
    cols = []
    for name, meta in t["columns"].items():
        kind = meta.get("pii")
        if kind:
            if kind not in MASK_KINDS:
                raise ValueError(f"{schema}.{t['table']}.{name}: unknown pii kind '{kind}' (use one of {', '.join(MASK_KINDS)})")
            cols.append(sql.SQL("{e} AS {n}").format(e=_masked_expr(name, kind, t["table"], t.get("primary_key")), n=sql.Identifier(name)))
        else:
            cols.append(sql.Identifier(name))
    return sql.SQL("CREATE VIEW {v} WITH (security_barrier = true) AS SELECT {cols} FROM {s}.{t} WHERE {w}").format(
        v=sql.Identifier("analytics", t["view"]), cols=sql.SQL(", ").join(cols),
        s=sql.Identifier(schema), t=sql.Identifier(t["table"]), w=_row_filter(t.get("rows", {})))


def load_spec(schema_or_path: str) -> dict:
    p = Path(schema_or_path)
    if not p.suffix:
        p = datasets_dir() / f"{schema_or_path}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def apply(conn: psycopg.Connection, spec: dict) -> list[str]:
    """Create / replace the governed views for one spec, in one transaction."""
    schema = spec["schema"]
    if schema in _SAFE_SYSTEM_SCHEMAS:
        raise ValueError(f"'{schema}' is a system or blueprint schema.")
    done: list[str] = []
    prefix = f"{schema}_".lower()
    with conn.transaction():
        cur = conn.cursor()
        # Drop this schema's views that are no longer in the spec, and their policies.
        wanted = {t["view"] for t in spec["tables"]}
        existing = [r[0] for r in cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'analytics' AND c.relkind = 'v' AND c.relname LIKE %s", (prefix + "%",)).fetchall()]
        for v in existing:
            if v not in wanted:
                cur.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(sql.Identifier("analytics", v)))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(OWNER_ROLE)))
        for t in spec["tables"]:
            if not t["view"].startswith(prefix):
                raise ValueError(f"view '{t['view']}' must start with '{prefix}' so it cannot replace another view")
            src = sql.Identifier(schema, t["table"])
            cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(src, sql.Identifier(OWNER_ROLE)))
            has_rls = cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = %s::regclass",
                                  (f'"{schema}"."{t["table"]}"',)).fetchone()[0]
            policy = sql.Identifier("nl2sql_owner_read")
            cur.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(policy, src))
            if has_rls:
                cur.execute(sql.SQL("CREATE POLICY {} ON {} FOR SELECT TO {} USING (true)").format(
                    policy, src, sql.Identifier(OWNER_ROLE)))
            v = sql.Identifier("analytics", t["view"])
            cur.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(v))
            cur.execute(build_view_sql(schema, t))
            cur.execute(sql.SQL("ALTER VIEW {} OWNER TO {}").format(v, sql.Identifier(OWNER_ROLE)))
            cur.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(v))
            cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(v, sql.Identifier(AGENT_ROLE)))
            done.append(t["view"])
    load_all.cache_clear()
    return done


# ---------------------------------------------------------------------------- catalog
@dataclass(frozen=True)
class DatasetView:
    schema: str
    view: str
    description: str
    columns: dict
    roles: tuple[str, ...]


@lru_cache(maxsize=1)
def load_all() -> tuple[tuple[dict, ...], tuple[DatasetView, ...]]:
    folder = datasets_dir()
    specs: list[dict] = []
    views: list[DatasetView] = []
    if folder.is_dir():
        for p in sorted(folder.glob("*.json")):
            try:
                spec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(spec, dict) or "tables" not in spec:
                continue
            specs.append(spec)
            for t in spec["tables"]:
                rows = role_rows(t.get("rows"))
                roles = tuple(r for r in ROLES if str(rows.get(r, "none")).strip().lower() not in ("", "none", "false"))
                views.append(DatasetView(spec["schema"], t["view"], t.get("description", ""), t["columns"], roles))
    return tuple(specs), tuple(views)


def _in_focus() -> tuple[tuple[dict, ...], tuple[DatasetView, ...]]:
    """The loaded specs and views narrowed to the current data selection (see focus.py)."""
    from . import focus

    key = focus.current()
    specs, views = load_all()
    if key == focus.ALL:
        return specs, views
    return tuple(s for s in specs if s["schema"] == key), tuple(v for v in views if v.schema == key)


def catalog_entries() -> dict:
    """Dataset views in the same shape as catalog.json's views."""
    out = {}
    for v in _in_focus()[1]:
        cols = {}
        for name, meta in v.columns.items():
            entry = {"type": meta.get("type", "text"), "description": meta.get("description", "")}
            if meta.get("pii"):
                entry["pii"] = True
                entry["mask"] = meta["pii"]
            cols[name] = entry
        out[v.view] = {"description": v.description, "roles": list(v.roles), "columns": cols, "dataset": v.schema}
    return out


def notes() -> list[str]:
    return [f"{s['schema']}: {s['notes']}" for s in _in_focus()[0] if s.get("notes")]


def vocabulary() -> set[str]:
    """Words from dataset table and column names (for screens that must not misfire)."""
    words: set[str] = set()
    for v in _in_focus()[1]:
        for name in [v.view, *v.columns]:
            for w in re.split(r"[_\W]+", name.lower()):
                if len(w) >= 4:
                    words.add(w)
                    if w.endswith("s"):
                        words.add(w[:-1])
    return words


# ---------------------------------------------------------------------------- example questions
_MONEY = re.compile(r"amount|balance|outstanding|value|limit|principal|exposure|price|total", re.I)
_VALUES = re.compile(r"Values: (.+)\.$")
_GROUPING = re.compile(r"type|status|segment|category|product|region|currency|channel|class|grade|stage", re.I)
_CONTACT = re.compile(r"email|phone|address|city", re.I)


def _words(name: str) -> str:
    return name.replace("_", " ").strip()


def _first_values(desc: str) -> list[str]:
    m = _VALUES.search(desc or "")
    return re.findall(r"'([^']*)'", m.group(1)) if m else []


def example_questions(schema: str, catalog: dict, year: int, limit: int = 10) -> list[list[str]]:
    """Example questions for the console, as [tag, question] pairs.

    A spec's own "examples" (strings, or [tag, question] pairs) win; otherwise they are
    built from the tables and columns this user can see, so each one is answerable.
    """
    for spec in load_all()[0]:
        if spec["schema"] == schema and spec.get("examples"):
            return [[e[0], e[1]] if isinstance(e, (list, tuple)) else ["answer", str(e)] for e in spec["examples"]][:limit]

    order = {t["view"]: i for spec in load_all()[0] if spec["schema"] == schema for i, t in enumerate(spec["tables"])}
    views = sorted((v for v in catalog if v in order), key=order.get)
    first: list[list[str]] = []   # each table's best question first, for variety
    more: list[list[str]] = []
    pii_example: list[str] | None = None
    for view in views:
        noun = _words(view[len(schema) + 1:] if view.startswith(schema + "_") else view)
        cols = catalog[view]["columns"]
        cats = [c for c, m in cols.items() if not m.get("pii") and not c.endswith(("_id", "_name", "name"))
                and _first_values(m.get("description", ""))]
        cats.sort(key=lambda c: 0 if _GROUPING.search(c) else 1)
        money = [c for c, m in cols.items() if not m.get("pii") and _MONEY.search(c)
                 and re.match(r"(numeric|decimal|double|real|integer|bigint|money)", m.get("type", ""))]
        dates = [c for c, m in cols.items() if m.get("type", "").startswith(("date", "timestamp")) and not m.get("pii")]
        has_active = any("active" in [x.lower() for x in _first_values(cols[c].get("description", ""))] for c in cats)
        qs = []
        if money and cats:
            qs.append(f"What is the total {_words(money[0])} by {_words(cats[0])}?")
        if has_active:
            qs.append(f"How many {noun} are active?")
        if money and not cats:
            qs.append(f"What is the total {_words(money[0])} across all {noun}?")
        if cats and not money:
            qs.append(f"How many {noun} are there by {_words(cats[0])}?")
        if dates:
            qs.append(f"How many {noun} by month of {_words(dates[0])} in {year}?")
        if not qs:
            qs.append(f"How many {noun} are there?")
        first.append(["answer", qs[0]])
        more.extend(["answer", q] for q in qs[1:])
        pii = [c for c, m in cols.items() if m.get("pii")]
        contact = [c for c in pii if _CONTACT.search(c)]
        if contact and (pii_example is None or pii_example[0] == "fallback"):
            pii_example = ["pii", f"List {noun} with their {_words(contact[0])}"]
        elif pii and pii_example is None:
            pii_example = ["fallback", f"List {noun} with their {_words(pii[0])}"]
    out = first + more
    if pii_example:
        out = out[: limit - 1] + [["pii", pii_example[1]]]
    seen: set[str] = set()
    return [e for e in out if not (e[1] in seen or seen.add(e[1]))][:limit]
