"""Bring-your-own-schema: propose → apply → governed views, end to end."""

from __future__ import annotations

import json
from datetime import date

import psycopg
import pytest

from sql_agent import datasets, knowledge, screens
from sql_agent.agent import Agent
from sql_agent.executor import ExecutionError, Executor
from sql_agent.model_backends import MockBackend
from sql_agent.planner import Plan
from sql_agent.validator import validate

SCHEMA = "test_bank"


def test_guess_pii():
    assert datasets.guess_pii("customers", "full_name") == "name"
    assert datasets.guess_pii("customers", "name") == "name"
    assert datasets.guess_pii("branches", "branch_name") is None
    assert datasets.guess_pii("products", "name") is None
    assert datasets.guess_pii("accounts", "account_number") == "account"
    assert datasets.guess_pii("accounts", "iban") == "account"
    assert datasets.guess_pii("customers", "ssn") == "id_number"
    assert datasets.guess_pii("customers", "mobile_phone") == "phone"
    assert datasets.guess_pii("customers", "date_of_birth") == "dob"
    assert datasets.guess_pii("customers", "zip_code") == "postal"
    assert datasets.guess_pii("accounts", "balance") is None
    assert datasets.guess_pii("transactions", "amount") is None
    assert datasets.guess_pii("customer_contacts", "phone_type") is None
    assert datasets.guess_pii("customer_contacts", "email_verified") is None
    assert datasets.guess_pii("customer_contacts", "is_primary_address") is None
    assert datasets.guess_pii("customer_contacts", "postal_code") == "postal"
    assert datasets.guess_pii("customers", "tax_id_masked") == "id_number"


@pytest.fixture(scope="module")
def bank(migrated, tmp_path_factory):
    folder = tmp_path_factory.mktemp("ds")
    mp = pytest.MonkeyPatch()
    mp.setenv("DATASETS_DIR", str(folder))
    datasets.load_all.cache_clear()
    with psycopg.connect(migrated.database_url, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"""
            CREATE SCHEMA {SCHEMA};
            CREATE TABLE {SCHEMA}.branches (branch_id int PRIMARY KEY, branch_name text, region text);
            CREATE TABLE {SCHEMA}.customers (customer_id int PRIMARY KEY, full_name text, email text, phone text,
                address text, city text, date_of_birth date, ssn text, branch_id int REFERENCES {SCHEMA}.branches);
            CREATE TABLE {SCHEMA}.accounts (account_id int PRIMARY KEY, customer_id int REFERENCES {SCHEMA}.customers,
                account_number text, account_type text, status text, balance numeric(12,2));
            INSERT INTO {SCHEMA}.branches VALUES (1, 'Seattle', 'West'), (2, 'Boston', 'East');
            INSERT INTO {SCHEMA}.customers VALUES
                (1, 'Liam Khan', 'liam.khan@example.com', '+1-206-555-0101', '101 Pine St', 'Seattle', '1980-02-07', '123456789', 1),
                (2, 'Ava Reyes', 'ava.reyes@example.com', '+1-617-555-0102', '9 Elm St', 'Boston', '1975-11-30', '987654321', 2);
            INSERT INTO {SCHEMA}.accounts VALUES (1, 1, 'GB29NWBK60161331920001', 'checking', 'active', 1500.00),
                (2, 1, 'GB29NWBK60161331920002', 'savings', 'dormant', 200.00), (3, 2, 'GB29NWBK60161331920003', 'checking', 'active', 9000.00);
            ALTER TABLE {SCHEMA}.customers ENABLE ROW LEVEL SECURITY;
        """)
        spec = datasets.propose(c, SCHEMA)
        rule = f"branch_id IN (SELECT b.branch_id FROM {SCHEMA}.branches b WHERE b.region = ctx.region())"
        for t in spec["tables"]:
            t["rows"]["sales_rep"] = "all"  # exercise the partial tier
            t["rows"]["supervisor_officer"] = {
                "branches": "region = ctx.region()", "customers": rule,
                "accounts": f"customer_id IN (SELECT c.customer_id FROM {SCHEMA}.customers c WHERE c.{rule})",
            }[t["table"]]
        datasets.write_spec(spec)
        datasets.apply(c, spec)
    yield spec
    with psycopg.connect(migrated.database_url, autocommit=True) as c:
        for t in spec["tables"]:
            c.execute(f'DROP VIEW IF EXISTS analytics."{t["view"]}"')
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    mp.undo()
    datasets.load_all.cache_clear()


@pytest.mark.db
def test_propose_spec(bank):
    by = {t["table"]: t for t in bank["tables"]}
    assert set(by) == {"branches", "customers", "accounts"}
    pii = {c: m["pii"] for c, m in by["customers"]["columns"].items() if m.get("pii")}
    assert pii == {"full_name": "name", "email": "email", "phone": "phone", "address": "address", "city": "city",
                   "date_of_birth": "dob", "ssn": "id_number"}
    assert by["accounts"]["columns"]["account_number"]["pii"] == "account"
    assert "'checking'" in by["accounts"]["columns"]["account_type"]["description"]
    assert "Joins test_bank_customers.customer_id" in by["accounts"]["columns"]["customer_id"]["description"]
    assert by["customers"]["source_has_rls"] is True
    assert by["customers"]["primary_key"] == "customer_id"


@pytest.mark.db
def test_rows_and_masking_per_user(bank, migrated):
    ex = Executor(migrated)
    sql = f"SELECT full_name, email, phone, address, city, date_of_birth, ssn FROM analytics.{SCHEMA}_customers ORDER BY customer_id"
    lena = ex.run("lena", sql, max_rows=10).rows
    sam = ex.run("sam", sql, max_rows=10).rows
    dev = ex.run("dev", sql, max_rows=10).rows
    priya = ex.run("priya", sql, max_rows=10).rows
    tom = ex.run("tom", sql, max_rows=10).rows
    assert lena[0] == ("Liam Khan", "liam.khan@example.com", "+1-206-555-0101", "101 Pine St", "Seattle", "1980-02-07", "123456789")
    assert sam[0] == ("Customer #1", "[REDACTED]", "[REDACTED]", "[REDACTED]", "[REDACTED]", "[REDACTED]", "[REDACTED]")
    assert dev[0] == ("Liam K.", "l***@example.com", "***-***-0101", "[REDACTED]", "Seattle", "1980", "****6789")
    assert len(priya) == 1 and priya[0][0] == "Customer #1"  # West branch only
    assert tom == []
    acct = f"SELECT account_number FROM analytics.{SCHEMA}_accounts ORDER BY account_id"
    assert ex.run("dev", acct, max_rows=5).rows[0] == ("****0001",)
    assert ex.run("sam", acct, max_rows=5).rows[0] == ("[REDACTED]",)
    assert len(ex.run("priya", acct, max_rows=5).rows) == 2
    with pytest.raises(ExecutionError):
        ex.run("lena", f"SELECT ssn FROM {SCHEMA}.customers", max_rows=5)


@pytest.mark.db
def test_catalog_validator_and_screens(bank, users):
    view = f"{SCHEMA}_customers"
    assert view in knowledge.catalog_for(users["sam"]) and view not in knowledge.catalog_for(users["tom"])
    assert knowledge.catalog_for(users["sam"])[view]["columns"]["ssn"]["you_see"] == "redacted"
    assert knowledge.catalog_for(users["dev"])[view]["columns"]["ssn"]["you_see"] == "partial"
    assert validate(f"SELECT customer_id, full_name FROM {view} LIMIT 5", users["sam"]).ok
    assert "pii_in_filter" in {h.rule for h in validate(f"SELECT customer_id FROM {view} WHERE ssn = '1'", users["sam"]).hits}
    assert "schema_not_allowed" in {h.rule for h in validate(f"SELECT ssn FROM {SCHEMA}.customers", users["lena"]).hits}
    assert "table_not_allowed" in {h.rule for h in validate(f"SELECT customer_id FROM {view}", users["tom"]).hits}
    # retail-only screens must not block banking questions
    assert screens.screen_ambiguity("How many active accounts are there?", None) is None
    assert screens.screen_ambiguity("Top 10 customers by account balance", None) is None
    assert screens.screen_pii("list the account numbers of my customers", users["priya"]).reason_code == "pii_above_tier"
    assert screens.screen_pii("show customer SSNs", users["sam"]).reason_code == "pii_above_tier"
    assert screens.screen_pii("show customer SSNs", users["lena"]) is None


class ScriptedModel(MockBackend):
    name = "scripted-test-model"

    def __init__(self, sql):
        self.sql = sql

    def plan(self, *a, **kw):
        return Plan("sql", sql=self.sql, presentation={})


@pytest.mark.db
def test_agent_answers_from_dataset(bank, migrated):
    sql = (f"SELECT c.customer_id, c.full_name, ROUND(SUM(a.balance), 2) AS total_balance FROM {SCHEMA}_accounts a "
           f"JOIN {SCHEMA}_customers c ON c.customer_id = a.customer_id GROUP BY c.customer_id, c.full_name ORDER BY total_balance DESC LIMIT 5")
    agent = Agent(migrated, backend=ScriptedModel(sql))
    lena = agent.ask(agent.user("lena"), "Which customers hold the most money across their accounts?", audit=False)
    priya = agent.ask(agent.user("priya"), "Which customers hold the most money across their accounts?", audit=False)
    assert lena.kind == "answer" and lena.rows[0][1] == "Ava Reyes" and len(lena.rows) == 2
    assert priya.kind == "answer" and [r[1] for r in priya.rows] == ["Customer #1"]


def test_retail_data_off_hides_the_retail_views(monkeypatch, users):
    monkeypatch.setenv("RETAIL_DATA", "off")
    assert "orders" not in knowledge.catalog_for(users["sam"])
    assert "table_not_allowed" in {h.rule for h in validate("SELECT order_id FROM orders", users["sam"]).hits}
    assert screens.screen_unsupported("what is our profit margin") is None  # no retail 'unsupported' list
    assert screens.screen_ambiguity("what's our revenue", None) is None    # no retail missing-period rule
    monkeypatch.setenv("RETAIL_DATA", "on")
    assert "orders" in knowledge.catalog_for(users["sam"])


@pytest.mark.db
def test_role_with_no_views_gets_a_plain_no_access_answer(monkeypatch, migrated):
    monkeypatch.setenv("RETAIL_DATA", "off")
    agent = Agent(migrated, backend=ScriptedModel("SELECT 1"))
    tom = agent.user("tom")
    if knowledge.views_for(tom):
        pytest.skip("a dataset in DATASETS_DIR grants this role a view")
    r = agent.ask(tom, "How many facilities are active?", audit=False)
    assert r.kind == "decline" and r.reason_code == "no_access" and "no access" in r.message


@pytest.mark.db
def test_data_selection_narrows_catalog_and_validator(bank, users, migrated):
    from sql_agent import focus

    keys = [o["key"] for o in focus.options()]
    assert keys[-1] == "retail" and SCHEMA in keys and "all" not in keys
    assert [o["label"] for o in focus.options() if o["key"] == "retail"] == ["Retail"]
    assert focus.default() == keys[0] != "retail"
    view = f"{SCHEMA}_customers"
    with focus.use(SCHEMA):
        cat = knowledge.catalog_for(users["sam"])
        assert view in cat and "orders" not in cat
        assert "table_not_allowed" in {h.rule for h in validate("SELECT order_id FROM orders", users["sam"]).hits}
        assert screens.screen_ambiguity("what's our revenue", None) is None  # retail-only rules are off
    with focus.use("retail"):
        cat = knowledge.catalog_for(users["sam"])
        assert "orders" in cat and view not in cat
        assert "table_not_allowed" in {h.rule for h in validate(f"SELECT customer_id FROM {view}", users["sam"]).hits}
    with focus.use("all"):
        assert {"orders", view} <= set(knowledge.catalog_for(users["sam"]))
    assert focus.current() == "all"
    with pytest.raises(ValueError):
        focus.normalize("nope")
    # the agent: a retail query is rejected when the question is about the bank dataset
    agent = Agent(migrated, backend=ScriptedModel("SELECT COUNT(*) AS n FROM orders"))
    r = agent.ask(agent.user("sam"), "How many orders?", audit=False, data=SCHEMA)
    assert r.kind != "answer" and any(h.rule == "table_not_allowed" for h in r.rules_fired)
    assert agent.ask(agent.user("sam"), "How many orders were placed in 2025?", audit=False, data="retail").kind == "answer"


@pytest.mark.db
def test_api_data_header(bank, migrated, monkeypatch):
    from fastapi.testclient import TestClient

    from sql_agent import api

    monkeypatch.setenv("DEMO_IDENTITY", "on")
    client = TestClient(api.app)
    h = {"X-Demo-User": "sam"}
    me = client.get("/api/me", headers={**h, "X-Data": SCHEMA}).json()
    assert me["data"] == SCHEMA and SCHEMA in [o["key"] for o in me["data_options"]] and me["retail_data"] is False
    assert "orders" not in client.get("/api/catalog", headers={**h, "X-Data": SCHEMA}).json()
    assert f"{SCHEMA}_customers" not in client.get("/api/catalog", headers={**h, "X-Data": "retail"}).json()
    assert client.get("/api/me", headers={**h, "X-Data": "nope"}).status_code == 400


def test_choosing_retail_shows_it_even_when_retail_data_is_off(monkeypatch, users):
    from sql_agent import focus

    monkeypatch.setenv("RETAIL_DATA", "off")
    assert "orders" not in knowledge.catalog_for(users["sam"])  # the combined view leaves it out
    with focus.use("retail"):
        assert "orders" in knowledge.catalog_for(users["sam"])
    assert "retail" in [o["key"] for o in focus.options()]


@pytest.mark.db
def test_example_questions_come_from_visible_tables(bank, users, monkeypatch):
    from sql_agent import focus

    with focus.use(SCHEMA):
        ex = datasets.example_questions(SCHEMA, knowledge.catalog_for(users["sam"]), 2026)
        tags = [t for t, _ in ex]
        qs = [q for _, q in ex]
        assert "What is the total balance by account type?" in qs
        assert "How many accounts are active?" in qs
        assert tags[-1] == "pii" and "email" in qs[-1]
        assert all(screens.run_screens(q, users["sam"], None)[0] is None for t, q in ex if t == "answer")
        assert datasets.example_questions(SCHEMA, knowledge.catalog_for(users["tom"]), 2026) == []
    # a spec's own examples win
    spec = json.loads(json.dumps(bank))
    spec["examples"] = ["How many branches are in the West?", ["pii", "Show customer SSNs"]]
    datasets.write_spec(spec)
    datasets.load_all.cache_clear()
    try:
        assert datasets.example_questions(SCHEMA, {}, 2026) == [
            ["answer", "How many branches are in the West?"], ["pii", "Show customer SSNs"]]
    finally:
        datasets.write_spec(bank)
        datasets.load_all.cache_clear()


def test_old_regional_manager_key_in_a_spec_still_works():
    rows = datasets.role_rows({"regional_manager": "region = ctx.region()", "analyst": "all"})
    assert rows == {"supervisor_officer": "region = ctx.region()", "analyst": "all"}
    assert datasets.role_rows({"regional_manager": "none", "supervisor_officer": "all"})["supervisor_officer"] == "all"
