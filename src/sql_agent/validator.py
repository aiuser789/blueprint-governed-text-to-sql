"""SQL validator — a policy check on the syntax tree, before execution.

The model's SQL is parsed with sqlglot (PostgreSQL dialect) and checked
against rules that depend on WHO is asking. It is not the security
boundary — PostgreSQL is (RLS, masked views, grants, read-only
transactions) — but it turns most bad queries into a clear message the
model can fix, and it means the database never even sees them.

What executes is the validator's regenerated SQL, not the model's raw
text. That closes the "parser differential" gap: a string that sqlglot
reads one way and PostgreSQL another cannot slip through, because
PostgreSQL only ever receives the tree that was checked.

Rules (code → meaning):

  parse_error           not parseable as PostgreSQL
  multiple_statements   more than one statement (``SELECT 1; DROP …``)
  not_select            anything but a read: DML, DDL, SET, COPY,
                        transactions, SELECT INTO, FOR UPDATE, EXPLAIN …
  schema_not_allowed    a schema other than ``analytics`` (``core.``,
                        ``private.``, ``pg_catalog.``, ``information_schema.``)
  table_not_allowed     a view the asker's role may not query, or any
                        name that is not one of their views
  function_not_allowed  a function outside the allowlist (``set_config``,
                        ``current_setting``, ``pg_sleep``, ``pg_read_file``,
                        ``dblink``, ``generate_series`` …)
  select_star           ``*`` outside ``COUNT(*)`` — name the columns
  pii_in_filter         a PII column the asker's tier cannot see in a
                        WHERE / JOIN ON / HAVING / ORDER BY / window
                        clause: the filter would run on the masked value
                        and return wrong rows (and it reads as a probe)
  pii_distinct_count    ``COUNT(DISTINCT <pii>)`` below full tier — the
                        count of distinct masked values is meaningless

Row cap: a passing query gets ``LIMIT max_rows + 1`` applied (or its own
smaller limit kept), so the executor can tell the user the result was
truncated instead of silently cutting it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from . import knowledge

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # "falling back to Command" notices are expected input
from .schemas import User

ALLOWED_FUNCTIONS = {
    # aggregates
    "COUNT", "SUM", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
    "PERCENTILE_CONT", "PERCENTILE_DISC", "GROUP_CONCAT", "ARRAY_AGG", "LOGICAL_AND", "LOGICAL_OR",
    # windows
    "ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST", "NTILE", "LAG", "LEAD",
    "FIRST_VALUE", "LAST_VALUE",
    # scalar
    "ROUND", "COALESCE", "NULLIF", "GREATEST", "LEAST", "ABS", "CEIL", "FLOOR", "TRUNC", "SIGN",
    "POWER", "SQRT", "EXP", "LN", "LOG", "CAST", "TRY_CAST", "CASE", "IF",
    "LOWER", "UPPER", "INITCAP", "LENGTH", "LEFT", "RIGHT", "SUBSTRING", "TRIM", "CONCAT", "SPLIT_PART",
    # dates
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "EXTRACT", "TIMESTAMP_TRUNC", "DATE_TRUNC", "TIME_TO_STR",
    "make_date", "age", "date_part", "to_char",
    # predicates sqlglot models as functions
    "EXISTS",
}

_WRITE_NODES = tuple(
    getattr(exp, name)
    for name in ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "Copy",
                 "Set", "Command", "Transaction", "Commit", "Rollback", "Grant", "Revoke", "Use",
                 "Pragma", "TruncateTable", "Into", "Lock", "LoadData", "Analyze", "Describe", "Refresh",
                 "Cache", "Uncache", "Kill", "Comment")
    if hasattr(exp, name)
)
_READ_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)


@dataclass
class Hit:
    rule: str
    message: str


@dataclass
class ValidationResult:
    ok: bool
    sql: str | None = None
    hits: list[Hit] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        return "\n".join(f"- {h.rule}: {h.message}" for h in self.hits)


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    return node.sql_name()


def _allowed(name: str) -> bool:
    return name in ALLOWED_FUNCTIONS or name.upper() in ALLOWED_FUNCTIONS or name.lower() in ALLOWED_FUNCTIONS


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _clause_columns(tree: exp.Expression) -> list[tuple[str, exp.Column]]:
    found: list[tuple[str, exp.Column]] = []
    for clause_type, label in ((exp.Where, "WHERE"), (exp.Having, "HAVING"), (exp.Order, "ORDER BY")):
        for clause in tree.find_all(clause_type):
            # ORDER BY inside an aggregate / window is still ordering on the value.
            for col in clause.find_all(exp.Column):
                found.append((label, col))
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            for col in on.find_all(exp.Column):
                found.append(("JOIN ON", col))
    for window in tree.find_all(exp.Window):
        for part in window.args.get("partition_by") or []:
            for col in part.find_all(exp.Column):
                found.append(("PARTITION BY", col))
    return found


def validate(sql: str, user: User, *, max_rows: int = 1000) -> ValidationResult:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return ValidationResult(False, hits=[Hit("parse_error", "Empty query.")])
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None and not isinstance(s, exp.Semicolon)]
    except ParseError as e:
        return ValidationResult(False, hits=[Hit("parse_error", f"Could not parse the SQL: {str(e).splitlines()[0][:200]}")])
    except Exception as e:  # sqlglot raises a few other types on odd input
        return ValidationResult(False, hits=[Hit("parse_error", f"Could not parse the SQL: {type(e).__name__}")])
    if not statements:
        return ValidationResult(False, hits=[Hit("parse_error", "No statement found.")])
    if len(statements) > 1:
        return ValidationResult(False, hits=[Hit("multiple_statements", "Send exactly one SELECT statement.")])
    tree = statements[0]
    hits: list[Hit] = []

    if not isinstance(tree, _READ_ROOTS):
        hits.append(Hit("not_select", f"Only a single read-only SELECT is allowed (got {type(tree).__name__.upper()})."))
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            hits.append(Hit("not_select", f"{type(node).__name__.upper()} is not allowed; the agent is read-only."))
            break

    views = knowledge.views_for(user)
    ctes = _cte_names(tree)
    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        catalog = (table.catalog or "").lower()
        if not name:  # a table function such as generate_series(); the function rule reports it
            continue
        if catalog or (schema and schema != "analytics"):
            hits.append(Hit("schema_not_allowed", f"'{table.sql(dialect='postgres')}' is outside the analytics schema. Use only the listed views, unqualified."))
            continue
        if name in ctes and not schema:
            continue
        if name not in views:
            if name in knowledge.all_views():
                hits.append(Hit("table_not_allowed", f"Your role ({user.role}) cannot query '{name}'. Available: {', '.join(sorted(views))}."))
            else:
                hits.append(Hit("table_not_allowed", f"'{name}' is not one of your views. Available: {', '.join(sorted(views))}."))
            continue
        tables.append(name)

    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Connector):  # AND / OR / XOR are operators, not functions
            continue
        name = _func_name(func)
        if not _allowed(name):
            hits.append(Hit("function_not_allowed", f"Function {name}() is not allowed."))

    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            hits.append(Hit("select_star", "Name the columns you need instead of using *."))
            break

    if user.pii_tier != "full":
        pii = knowledge.pii_columns()
        ok_cols = knowledge.filterable_pii(user.pii_tier)
        reported: set[tuple[str, str]] = set()
        for where, col in _clause_columns(tree):
            cname = (col.name or "").lower()
            if cname in pii and cname not in ok_cols and (where, cname) not in reported:
                reported.add((where, cname))
                hits.append(Hit("pii_in_filter",
                                f"{cname} is masked at your access level ({user.pii_tier}), so it cannot be used in {where}: "
                                "the database would compare against the masked value and return wrong rows. Use customer_id, region or segment instead."))
        for count in tree.find_all(exp.Count):
            inner = count.this
            if isinstance(inner, exp.Distinct):
                for col in inner.find_all(exp.Column):
                    cname = (col.name or "").lower()
                    if cname in pii and cname not in ok_cols:
                        hits.append(Hit("pii_distinct_count",
                                        f"COUNT(DISTINCT {cname}) counts masked values at your access level; count customer_id instead."))

    if hits:
        return ValidationResult(False, hits=hits, tables=tables)

    capped = _apply_row_cap(tree, max_rows + 1)
    return ValidationResult(True, sql=capped.sql(dialect="postgres", pretty=True, comments=False), tables=sorted(set(tables)))


def _apply_row_cap(tree: exp.Expression, cap: int) -> exp.Expression:
    if isinstance(tree, exp.Select):
        limit = tree.args.get("limit")
        current = None
        if limit is not None:
            try:
                current = int(limit.expression.this) if limit.expression is not None else None
            except (TypeError, ValueError, AttributeError):
                current = None
        if current is None or current > cap:
            return tree.limit(cap, copy=True)
        return tree
    return exp.select("*").from_(tree.subquery("capped")).limit(cap)
