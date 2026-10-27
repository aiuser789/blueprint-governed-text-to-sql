"""Offline planner, output scan and model-backend parsing: hermetic."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sql_agent import periods
from sql_agent.config import derive_agent_url, get_settings
from sql_agent.model_backends import AnthropicBackend, MockBackend, OpenAIBackend, parse_plan
from sql_agent.output_scan import scan_rows, scan_text
from sql_agent.planner import plan
from sql_agent.validator import validate

AS_OF = date(2026, 9, 30)


def _p(q, user):
    return plan(q, user, periods.resolve(q, AS_OF, 2), AS_OF)


def test_planner_output_always_validates(users):
    questions = [
        "What was total revenue last month?", "Top 5 customers by revenue last quarter", "Revenue by region in Q2 2026",
        "Revenue by category last quarter", "Top 10 products by units sold in August 2026", "Average order value by month in 2025",
        "Orders by status in September 2026", "How many customers do I have by segment?", "Top 5 sales reps by revenue in 2025",
        "How many new customers did we get in 2025?", "Show me East region revenue last month",
    ]
    for user_id in ("priya", "dev", "sam", "lena"):
        for q in questions:
            p = _p(q, users[user_id])
            assert p.action == "sql", (user_id, q, p.message)
            assert validate(p.sql, users[user_id]).ok, (user_id, q, validate(p.sql, users[user_id]).feedback())


def test_planner_excludes_cancelled_and_uses_order_date(users):
    sql = _p("What was total revenue last month?", users["sam"]).sql
    assert "status <> 'cancelled'" in sql and "order_date" in sql and "created_at" not in sql


def test_planner_groups_products_by_id(users):
    sql = _p("Top 10 products by units sold in August 2026", users["sam"]).sql
    assert "GROUP BY p.product_id" in sql


def test_contractor_gets_summaries_or_a_decline(users):
    p = _p("Revenue by region by month in Q3 2026", users["tom"])
    assert p.action == "sql" and "region_month_summary" in p.sql and validate(p.sql, users["tom"]).ok
    assert _p("Top 10 customers by revenue last month", users["tom"]).reason_code == "role_scope"


def test_unknown_question_is_an_honest_decline(users):
    p = _p("Which weekday has the most rain?", users["sam"])
    assert p.action == "decline" and p.reason_code == "not_recognised"


def test_output_scan():
    assert not scan_text("Reach Jane at jane.doe@example.com").clean
    assert not scan_text("Call +1-206-555-0182").clean
    assert not scan_text("She lives at 1234 Maple Ave").clean
    assert scan_text("Revenue was $1,234,567.89 across 801 orders in 2026-08.").clean
    assert scan_text("Top customer: Customer #3284 (j***@example.com, ***-***-0182)").clean
    res, bad = scan_rows(["customer_id", "email"], [[1, "jane.doe@example.com"], [2, "[REDACTED]"]])
    assert not res.clean and bad == {1}


def test_parse_plan():
    p = parse_plan('Sure! {"action": "sql", "sql": "SELECT 1", "definitions_used": ["revenue"]}')
    assert p.action == "sql" and p.sql == "SELECT 1" and p.definitions_used == ["revenue"]
    assert parse_plan('{"action": "clarify", "message": "Which year?", "options": ["2025", "2026"]}').action == "clarify"
    assert parse_plan('{"action": "decline", "reason_code": "unanswerable", "message": "No cost data."}').reason_code == "unanswerable"
    bad = parse_plan("I think you should SELECT * FROM customers")
    assert bad.action == "sql" and bad.sql is None and bad.reason_code == "malformed"


class _FakeAnthropic:
    def __init__(self, text):
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(text=text)]))


class _FakeOpenAI:
    def __init__(self, text):
        msg = SimpleNamespace(message=SimpleNamespace(content=text))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(choices=[msg])))


def test_llm_backends_with_fake_clients(users):
    s = get_settings()
    reply = '{"action": "sql", "sql": "SELECT COUNT(*) AS n FROM orders", "definitions_used": []}'
    for backend in (AnthropicBackend(s, client=_FakeAnthropic(reply)), OpenAIBackend(s, client=_FakeOpenAI(reply))):
        p = backend.plan("how many orders all time", users["sam"], None, AS_OF)
        assert p.sql == "SELECT COUNT(*) AS n FROM orders"


def test_prompt_is_role_trimmed(users):
    seen = {}

    class Capture(_FakeAnthropic):
        def __init__(self):
            def create(**kw):
                seen["prompt"] = kw["messages"][0]["content"]
                return SimpleNamespace(content=[SimpleNamespace(text='{"action": "decline", "message": "x"}')])
            self.messages = SimpleNamespace(create=create)

    AnthropicBackend(get_settings(), client=Capture()).plan("revenue", users["tom"], None, AS_OF)
    prompt = seen["prompt"]
    assert "region_month_summary" in prompt
    assert "view customers" not in prompt and "core." not in prompt and "private." not in prompt and "session" not in prompt.lower()


def test_mock_compose_handles_empty_and_null(users):
    from sql_agent.planner import Plan

    m = MockBackend()
    assert "No rows" in m.compose("q", users["sam"], Plan("sql", presentation={"kind": "scalar"}), ["revenue"], [], None, False)
    out = m.compose("q", users["sam"], Plan("sql", presentation={"kind": "scalar", "value": "revenue", "money": True}),
                    ["revenue", "orders"], [[None, 0]], "August 2026", False)
    assert "No matching orders" in out


def test_agent_url_derivation():
    assert derive_agent_url("postgresql://svc:pw@db:5432/nl2sql", "s3cret") == "postgresql://nl2sql_agent:s3cret@db:5432/nl2sql"
