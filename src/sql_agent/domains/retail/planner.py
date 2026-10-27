"""The retail offline planner — a deterministic stand-in for the model, for the retail data.

It maps a known set of business-question shapes to SQL over the
analytics views. It exists so the whole pipeline (screens, validation,
RLS, masking, output scan, audit, approvals) runs and is testable with
no API key. It is NOT a natural-language model: an unfamiliar phrasing
gets an honest "I don't recognise this question" decline. Text-to-SQL
quality is measured with a real backend (``make llm-evals``).

Every plan goes through exactly the same validator and executor as a
model's plan. The planner gets no shortcut past them.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ... import knowledge
from ...periods import Period
from ...planner import Plan
from ...schemas import User




def _n(q: str, default: int = 10) -> int:
    m = re.search(r"\btop\s+(\d{1,3})\b|\b(\d{1,3})\s+(?:best|biggest|largest|top)\b", q)
    if m:
        return max(1, min(int(m.group(1) or m.group(2)), 100))
    words = {"three": 3, "five": 5, "ten": 10, "twenty": 20}
    m = re.search(r"\btop\s+(three|five|ten|twenty)\b", q)
    return words[m.group(1)] if m else default


def _period_sql(period: Period | None, column: str) -> str:
    if period is None:
        return ""
    return period.sql_filter(column)


def _where(*parts: str) -> str:
    parts = tuple(p for p in parts if p)
    return ("WHERE " + " AND ".join(parts)) if parts else ""


def _month_filter(period: Period | None) -> str:
    """Summaries are monthly: filter on month with the period's month bounds."""
    if period is None or (period.start is None and period.end is None):
        return ""
    parts = []
    if period.start:
        parts.append(f"month >= DATE '{period.start.replace(day=1).isoformat()}'")
    if period.end:
        parts.append(f"month < DATE '{period.end.isoformat()}'")
    return " AND ".join(parts)


_REGIONS = ("North", "South", "East", "West")


def _region_filter(q: str, column: str) -> str:
    """A region the question names becomes a filter. RLS still decides what
    is visible: a West manager asking about East gets an honest empty result
    (plus a scope note), never West's numbers under an East label."""
    named = [r for r in _REGIONS if re.search(rf"\b{r.lower()}(ern)?\b", q)]
    if not named or re.search(r"\b(by|per|each|across|all) regions?\b", q):
        return ""
    return f"{column} IN (" + ", ".join(f"'{r}'" for r in named) + ")"


def _decline_row_level(user: User, what: str) -> Plan:
    return Plan(
        "decline", reason_code="role_scope",
        message=(f"Your role ({user.role}) has no access to row-level {what}. I can answer from the monthly region and "
                 "category summaries instead — for example revenue, orders or units by region, category or month."),
    )


def plan(question: str, user: User, period: Period | None, as_of: date) -> Plan:
    q = " " + question.lower().strip().rstrip("?.!") + " "
    views = knowledge.views_for(user)
    contractor = "orders" not in views
    not_cancelled = "o.status <> 'cancelled'"
    pf = " AND ".join(p for p in (_period_sql(period, "o.order_date"), _region_filter(q, "o.region")) if p)

    by_region = bool(re.search(r"\b(by|per|each|across) regions?\b|\bregion by region\b|\bregional breakdown\b", q))
    by_month = bool(re.search(r"\b(by|per|each) month\b|\bmonthly\b|\bmonth by month\b|\btrend\b|\bover time\b", q))
    by_category = bool(re.search(r"\b(by|per|each) (product )?categor(y|ies)\b|\bcategory breakdown\b|\bwhich categor", q))
    by_segment = bool(re.search(r"\b(by|per|each) segments?\b|\bsegment breakdown\b", q))
    by_status = bool(re.search(r"\b(by|per) status\b|\bstatus breakdown\b|\beach status\b", q))
    metric_units = bool(re.search(r"\bunits?\b|\bitems sold\b|\bquantity\b|\bbest[- ]selling\b|\bmost sold\b", q))

    # ---- active customers (definition disambiguated by the screen) ---------------
    if re.search(r"\bactive (customers?|accounts?)\b", q):
        if contractor:
            return _decline_row_level(user, "customer data")
        days = 90 if re.search(r"90|ninety", q) else None
        start = (as_of - timedelta(days=89)) if days else date(as_of.year - 1, as_of.month, as_of.day) + timedelta(days=1)
        key = "90-day" if days else "12-month"
        return Plan("sql", sql=(
            "SELECT COUNT(DISTINCT o.customer_id) AS active_customers\n"
            f"FROM orders o\nWHERE {not_cancelled} AND o.order_date >= DATE '{start.isoformat()}' AND o.order_date <= DATE '{as_of.isoformat()}'"),
            definitions_used=[f"active customer ({key})"],
            presentation={"kind": "scalar", "label": f"Active customers ({key} definition, as of {as_of.isoformat()})"})

    # ---- top customers ------------------------------------------------------------
    if re.search(r"\b(top|best|biggest|largest|highest[- ]spending|most valuable)\b.{0,20}\bcustomers\b", q):
        if contractor:
            return _decline_row_level(user, "customer data")
        n = _n(q)
        if re.search(r"\b(by|most) (number of )?orders\b|\border count\b", q):
            return Plan("sql", sql=(
                "SELECT c.customer_id, c.full_name, c.region, COUNT(*) AS orders\n"
                "FROM orders o\nJOIN customers c ON c.customer_id = o.customer_id\n"
                f"{_where(not_cancelled, pf)}\nGROUP BY c.customer_id, c.full_name, c.region\n"
                f"ORDER BY orders DESC, c.customer_id\nLIMIT {n}"),
                definitions_used=["order count"], presentation={"kind": "ranking", "label": f"Top {n} customers by orders", "value": "orders", "name": "full_name"})
        return Plan("sql", sql=(
            "SELECT c.customer_id, c.full_name, c.region, ROUND(SUM(o.total_amount), 2) AS revenue, COUNT(*) AS orders\n"
            "FROM orders o\nJOIN customers c ON c.customer_id = o.customer_id\n"
            f"{_where(not_cancelled, pf)}\nGROUP BY c.customer_id, c.full_name, c.region\n"
            f"ORDER BY revenue DESC, c.customer_id\nLIMIT {n}"),
            definitions_used=["revenue"], presentation={"kind": "ranking", "label": f"Top {n} customers by revenue", "value": "revenue", "name": "full_name", "money": True})

    # ---- top products -------------------------------------------------------------
    if re.search(r"\b(top|best|most popular|best[- ]selling|most sold|highest[- ]grossing)\b.{0,20}\bproducts?\b", q):
        if contractor:
            return _decline_row_level(user, "product-level sales (only category summaries)")
        n = _n(q)
        value = "units" if metric_units and "revenue" not in q else "revenue"
        expr = "SUM(i.quantity)" if value == "units" else "ROUND(SUM(i.quantity * i.unit_price), 2)"
        return Plan("sql", sql=(
            f"SELECT p.product_id, p.name, p.category, {expr} AS {value}\n"
            "FROM order_items i\nJOIN orders o ON o.order_id = i.order_id\nJOIN products p ON p.product_id = i.product_id\n"
            f"{_where(not_cancelled, pf)}\nGROUP BY p.product_id, p.name, p.category\n"
            f"ORDER BY {value} DESC, p.product_id\nLIMIT {n}"),
            definitions_used=["units sold" if value == "units" else "revenue"],
            presentation={"kind": "ranking", "label": f"Top {n} products by {value}", "value": value, "name": "name", "extra": "category", "money": value == "revenue"})

    # ---- top reps -------------------------------------------------------------------
    if re.search(r"\b(top|best)\b.{0,15}\b(sales )?reps?\b|\breps? by revenue\b|\brevenue by rep\b", q):
        if contractor:
            return _decline_row_level(user, "sales rep data")
        n = _n(q, 5)
        return Plan("sql", sql=(
            "SELECT r.rep_id, r.rep_name, r.region, ROUND(SUM(o.total_amount), 2) AS revenue\n"
            "FROM orders o\nJOIN customers c ON c.customer_id = o.customer_id\nJOIN sales_reps r ON r.rep_id = c.assigned_rep_id\n"
            f"{_where(not_cancelled, pf)}\nGROUP BY r.rep_id, r.rep_name, r.region\nORDER BY revenue DESC, r.rep_id\nLIMIT {n}"),
            definitions_used=["revenue"],
            presentation={"kind": "ranking", "label": f"Top {n} reps by revenue (customer's current rep)", "value": "revenue", "name": "rep_name", "money": True})

    # ---- customer counts (state, no period needed) -------------------------------------
    if re.search(r"\bnew customers\b|\bsign[- ]?ups\b|\bcustomers (signed up|joined)\b", q):
        if contractor:
            return _decline_row_level(user, "customer data")
        pc = _period_sql(period, "created_at")
        if by_month:
            return Plan("sql", sql=(
                "SELECT date_trunc('month', created_at)::date AS month, COUNT(*) AS new_customers\n"
                f"FROM customers\n{_where(pc)}\nGROUP BY 1\nORDER BY 1"),
                definitions_used=["new customers"], presentation={"kind": "series", "label": "New customers by month", "value": "new_customers", "x": "month"})
        return Plan("sql", sql=f"SELECT COUNT(*) AS new_customers\nFROM customers\n{_where(pc)}",
                    definitions_used=["new customers"], presentation={"kind": "scalar", "label": "New customers"})

    if re.search(r"\b(age bands?|age groups?)\b", q):
        if contractor:
            return _decline_row_level(user, "customer data")
        return Plan("sql", sql="SELECT age_band, COUNT(*) AS customers\nFROM customers\nGROUP BY age_band\nORDER BY age_band",
                    presentation={"kind": "breakdown", "label": "Customers by age band", "value": "customers", "x": "age_band"})

    if re.search(r"\b(how many customers|number of customers|customer count|count of customers|customers do (i|we) have|customers by (segment|region))\b", q) and not re.search(r"\b(ordered|placed|bought|purchased)\b", q):
        if contractor:
            return _decline_row_level(user, "customer data")
        if by_segment or "by segment" in q:
            return Plan("sql", sql="SELECT segment, COUNT(*) AS customers\nFROM customers\nGROUP BY segment\nORDER BY customers DESC",
                        presentation={"kind": "breakdown", "label": "Customers by segment", "value": "customers", "x": "segment"})
        if by_region or "by region" in q:
            return Plan("sql", sql="SELECT region, COUNT(*) AS customers\nFROM customers\nGROUP BY region\nORDER BY region",
                        presentation={"kind": "breakdown", "label": "Customers by current region", "value": "customers", "x": "region"})
        return Plan("sql", sql=f"SELECT COUNT(*) AS customers\nFROM customers\n{_where(_region_filter(q, 'region'))}",
                    presentation={"kind": "scalar", "label": "Customers"})

    if re.search(r"\blist (my |all |the |our )?customers\b|\bshow (me )?(my |all |the )?customers\b", q):
        if contractor:
            return _decline_row_level(user, "customer data")
        n = _n(q, 20)
        return Plan("sql", sql=f"SELECT customer_id, full_name, city, segment, region\nFROM customers\nORDER BY customer_id\nLIMIT {n}",
                    presentation={"kind": "list", "label": f"First {n} customers by ID"})

    # ---- order listings (can exceed the row cap -> export offer) ---------------------
    if re.search(r"\b(list|show|export|download|give me)\b.{0,15}\b(all |every |the )?orders\b|\bevery order\b|\ball (the )?orders\b(?!.*\b(count|how many|number)\b)", q) and not re.search(r"\bhow many\b|\bnumber of\b|\bby status\b", q):
        if contractor:
            return _decline_row_level(user, "order data")
        return Plan("sql", sql=(
            "SELECT o.order_id, o.customer_id, o.region, o.order_date, o.status, o.total_amount\n"
            f"FROM orders o\n{_where(pf)}\nORDER BY o.order_date, o.order_id"),
            presentation={"kind": "list", "label": "Orders"})

    # ---- status ---------------------------------------------------------------------------
    if by_status or re.search(r"\bcancell?ed orders\b|\bcancell?ation(s| rate)?\b", q):
        if contractor:
            return _decline_row_level(user, "order status data")
        if by_status:
            return Plan("sql", sql=(
                f"SELECT o.status, COUNT(*) AS orders\nFROM orders o\n{_where(pf)}\nGROUP BY o.status\nORDER BY orders DESC"),
                presentation={"kind": "breakdown", "label": "Orders by status", "value": "orders", "x": "status"})
        return Plan("sql", sql=(
            "SELECT COUNT(*) FILTER (WHERE o.status = 'cancelled') AS cancelled_orders, COUNT(*) AS all_orders,\n"
            "       ROUND(100.0 * COUNT(*) FILTER (WHERE o.status = 'cancelled') / NULLIF(COUNT(*), 0), 1) AS cancellation_rate_pct\n"
            f"FROM orders o\n{_where(pf)}"),
            presentation={"kind": "row", "label": "Cancellations"})

    # ---- metrics: revenue / orders / AOV / units ----------------------------------------------
    is_aov = bool(re.search(r"\baverage order value\b|\baov\b|\baverage order\b|\bavg order\b|\baverage basket\b", q))
    is_orders = bool(re.search(r"\bhow many orders\b|\bnumber of orders\b|\border count\b|\bcount of orders\b|\borders (did|were|have)\b|\btotal orders\b", q))
    is_units = bool(re.search(r"\bunits sold\b|\bhow many units\b|\bitems sold\b|\bunits\b", q))
    is_revenue = bool(re.search(r"\brevenue\b|\bsales\b|\bturnover\b|\bgmv\b|\bearn(ed)?\b|\bsell\b", q))
    if not (is_aov or is_orders or is_units or is_revenue):
        return Plan(
            "decline", reason_code="not_recognised",
            message=("The offline planner doesn't recognise this question. It knows revenue, orders, average order value, units, "
                     "top customers / products / reps, customer counts, new and active customers and status breakdowns — by "
                     "region, month, category or segment. With a model backend configured (MODEL_BACKEND), any question over "
                     "the same views can be tried, under the same controls."),
        )

    if is_units:
        value_sql, value, defs = "SUM(i.quantity)", "units", ["units sold"]
    elif is_aov:
        value_sql, value, defs = "ROUND(AVG(o.total_amount), 2)", "avg_order_value", ["average order value"]
    elif is_orders:
        value_sql, value, defs = "COUNT(*)", "orders", ["order count"]
    else:
        value_sql, value, defs = "ROUND(SUM(o.total_amount), 2)", "revenue", ["revenue"]
    money = value in {"revenue", "avg_order_value"}

    if contractor:
        return _plan_from_summaries(q, period, value, defs, by_region, by_month, by_category, user)

    needs_items = is_units or by_category
    if needs_items:
        if value == "revenue":
            value_sql = "ROUND(SUM(i.quantity * i.unit_price), 2)"
        elif value == "orders":
            value_sql = "COUNT(DISTINCT o.order_id)"
        elif value == "avg_order_value":
            value_sql = "ROUND(SUM(i.quantity * i.unit_price) / NULLIF(COUNT(DISTINCT o.order_id), 0), 2)"
        frm = "FROM order_items i\nJOIN orders o ON o.order_id = i.order_id\nJOIN products p ON p.product_id = i.product_id"
    else:
        frm = "FROM orders o"

    dims: list[tuple[str, str]] = []
    if by_region:
        dims.append(("o.region", "region"))
    if by_month:
        dims.append(("date_trunc('month', o.order_date)::date", "month"))
    if by_category:
        dims.append(("p.category", "category"))
    if by_segment:
        frm += "\nJOIN customers c ON c.customer_id = o.customer_id"
        dims.append(("c.segment", "segment"))

    where = _where(not_cancelled, pf)
    if not dims:
        extra = ", COUNT(*) AS orders" if value == "revenue" and not needs_items else ""
        return Plan("sql", sql=f"SELECT {value_sql} AS {value}{extra}\n{frm}\n{where}", definitions_used=defs,
                    presentation={"kind": "scalar", "label": value.replace("_", " "), "value": value, "money": money})
    select_dims = ", ".join(f"{expr} AS {alias}" for expr, alias in dims)
    group = ", ".join(str(i + 1) for i in range(len(dims)))
    order = ", ".join(str(i + 1) for i in range(len(dims))) if (by_month or len(dims) > 1) else f"{value} DESC"
    return Plan("sql", sql=f"SELECT {select_dims}, {value_sql} AS {value}\n{frm}\n{where}\nGROUP BY {group}\nORDER BY {order}",
                definitions_used=defs,
                presentation={"kind": "series" if by_month and len(dims) == 1 else "breakdown",
                              "label": f"{value.replace('_', ' ')} by " + " and ".join(a for _, a in dims),
                              "value": value, "x": dims[0][1], "money": money})


def _plan_from_summaries(q: str, period: Period | None, value: str, defs: list[str], by_region: bool, by_month: bool,
                         by_category: bool, user: User) -> Plan:
    mf = " AND ".join(p for p in (_month_filter(period), _region_filter(q, "region")) if p)
    if by_category or value == "units":
        src, col = "category_month_summary", {"revenue": "SUM(revenue)", "units": "SUM(units)"}.get(value)
        if col is None:
            return _decline_row_level(user, "order-level data for that metric by category")
        dims = [d for d, on in (("region", by_region), ("month", by_month), ("category", by_category)) if on]
    else:
        src = "region_month_summary"
        col = {"revenue": "SUM(revenue)", "orders": "SUM(orders)",
               "avg_order_value": "ROUND(SUM(revenue) / NULLIF(SUM(orders), 0), 2)"}[value]
        dims = [d for d, on in (("region", by_region), ("month", by_month)) if on]
    if value in {"revenue", "avg_order_value"} and "ROUND" not in col:
        col = f"ROUND({col}, 2)"
    where = _where(mf)
    money = value in {"revenue", "avg_order_value"}
    if not dims:
        return Plan("sql", sql=f"SELECT {col} AS {value}\nFROM {src}\n{where}", definitions_used=defs + ["summary views"],
                    presentation={"kind": "scalar", "label": value.replace("_", " "), "value": value, "money": money})
    group = ", ".join(dims)
    return Plan("sql", sql=f"SELECT {group}, {col} AS {value}\nFROM {src}\n{where}\nGROUP BY {group}\nORDER BY {group}",
                definitions_used=defs + ["summary views"],
                presentation={"kind": "series" if dims == ["month"] else "breakdown",
                              "label": f"{value.replace('_', ' ')} by " + " and ".join(dims), "value": value, "x": dims[0], "money": money})
