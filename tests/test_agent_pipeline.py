"""End to end through the agent, including a deliberately hostile model."""

from __future__ import annotations

import pytest

from sql_agent.agent import Agent
from sql_agent.model_backends import MockBackend
from sql_agent.planner import Plan
from sql_agent.store import ApprovalError

pytestmark = pytest.mark.db


def test_same_question_different_people(agent):
    q = "Top 5 customers by revenue last quarter"
    priya, dev, lena = (agent.ask(agent.user(u), q) for u in ("priya", "dev", "lena"))
    assert {r[2] for r in priya.rows} == {"West"}
    assert all(r[1].startswith("Customer #") for r in priya.rows)
    assert all(r[1].endswith(".") for r in dev.rows)
    assert not any(r[1].startswith("Customer #") for r in lena.rows)
    assert priya.sql.split("LIMIT")[0] == lena.sql.split("LIMIT")[0]  # identical SQL, different answers


def test_out_of_scope_question_is_honest(agent):
    r = agent.ask(agent.user("priya"), "Show me East region revenue last month")
    assert r.kind == "answer" and r.rows[0][0] is None
    assert "West region data only" in r.scope_note
    assert "No matching orders" in r.message


def test_truncation_offers_export(agent):
    r = agent.ask(agent.user("sam"), "List all orders in 2025")
    assert r.truncated and r.row_count == agent.settings.max_result_rows and r.export_available


def test_audit_row_has_sql_and_counts_but_no_values(agent):
    r = agent.ask(agent.user("lena"), "Top 5 customers by revenue last quarter")
    row = [a for a in agent.store.audit(limit=5) if a["audit_id"] == r.audit_id][0]
    assert row["sql"] == r.sql and row["row_count"] == 5 and row["pii_tier"] == "full"
    blob = str(row)
    assert not any(str(cell) in blob for cell in r.rows[0][1:2])  # no customer name in the log


class HostileModel(MockBackend):
    """A compromised or prompt-injected model: it writes attacks, then a sneaky legal query."""

    name = "hostile-test-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def plan(self, question, user, period, as_of, **kw):
        self.calls += 1
        return Plan("sql", sql=self.script.pop(0) if self.script else None, presentation={})

    def compose(self, question, user, plan, columns, rows, period_label, truncated):
        return "Lead contact: jane.doe@example.com, 1234 Maple Ave, +1-206-555-0182."  # invented PII


def test_hostile_model_attacks_are_stopped(migrated):
    model = HostileModel([
        "SELECT full_name, email FROM core.customers",
        "SELECT set_config('app.session_token', 'x', true)",
        "DELETE FROM orders",
    ])
    agent = Agent(migrated, backend=model)
    r = agent.ask(agent.user("priya"), "What was total revenue last month?")
    assert r.kind == "decline" and r.reason_code == "no_safe_query"
    assert {h.rule for h in r.rules_fired} >= {"schema_not_allowed", "function_not_allowed", "not_select"}
    assert model.calls == 3 and not r.rows


def test_hostile_model_legal_sql_is_still_scoped_and_masked(migrated):
    model = HostileModel(["SELECT customer_id, full_name, email, region FROM customers ORDER BY customer_id LIMIT 50"])
    agent = Agent(migrated, backend=model)
    r = agent.ask(agent.user("priya"), "What was total revenue last month?")
    assert r.kind == "answer"
    assert {row[3] for row in r.rows} == {"West"}
    assert all(row[2] == "[REDACTED]" for row in r.rows)
    # its invented PII in the prose is caught and replaced
    assert r.output_scan.startswith("blocked_text") and "@" not in r.message and "Maple" not in r.message


def test_export_needs_a_different_approver_and_runs_as_requester(agent):
    priya, sam, marco = agent.user("priya"), agent.user("sam"), agent.user("marco")
    req = agent.request_export(priya, "List all orders in 2025", "Year-end reconciliation")
    rid = req["request_id"]
    with pytest.raises(ApprovalError):
        agent.run_export(rid, priya)                      # not approved yet
    with pytest.raises(ApprovalError):
        agent.store.decide_export(rid, priya, True, "self-approval")   # requester cannot approve
    with pytest.raises(ApprovalError):
        agent.store.decide_export(rid, marco, True, "not an approver")  # marco has no approval right
    agent.store.decide_export(rid, sam, True, "Checked purpose")
    with pytest.raises(ApprovalError):
        agent.run_export(rid, sam)                         # only the requester downloads
    text, n = agent.run_export(rid, priya)
    assert n > agent.settings.max_result_rows
    regions = {line.split(",")[2] for line in text.splitlines()[1:]}
    assert regions == {"West"}                             # requester's scope, not the approver's


def test_glossary_proposal_flow(agent):
    dev, sam = agent.user("dev"), agent.user("sam")
    p = agent.store.propose_term(dev, "repeat customer", "A customer with two or more non-cancelled orders in the period.",
                                 "HAVING COUNT(*) >= 2")
    with pytest.raises(ApprovalError):
        agent.store.decide_proposal(p["proposal_id"], dev, True, "mine")
    decided = agent.store.decide_proposal(p["proposal_id"], sam, True, "Matches the CRM definition")
    assert decided["status"] == "approved"
    assert any(t["term"] == "repeat customer" for t in agent.store.approved_terms())
