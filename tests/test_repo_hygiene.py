"""Repository invariants: hermetic."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_live_in_one_place():
    names = sorted(p.name for p in (ROOT / "src/sql_agent/migrations").glob("*.sql"))
    assert names and names == sorted(names) and all(re.match(r"\d{4}_[a-z_]+\.sql$", n) for n in names)
    assert not (ROOT / "migrations").exists()  # no second copy to drift out of sync


def test_golden_and_redteam_files_are_well_formed():
    golden = [json.loads(x) for x in (ROOT / "evals/golden.jsonl").read_text().splitlines() if x.strip()]
    assert len(golden) >= 60 and len({c["case_id"] for c in golden}) == len(golden)
    for c in golden:
        assert c["expect"] in {"answer", "clarify", "decline"}
        assert c["expect"] != "answer" or c.get("reference_sql")
    red = [json.loads(x) for x in (ROOT / "evals/redteam.jsonl").read_text().splitlines() if x.strip()]
    assert len(red) >= 25 and all(c["database"] in {"denied", "timeout", "safe"} for c in red)


def test_retail_data_is_synthetic():
    from sql_agent.domains.retail.seed import generate

    data = generate(n_customers=200, n_orders=500)
    assert all(c[2].endswith("@example.com") for c in data["customers"])
    assert all("-555-01" in c[3] for c in data["customers"])


def test_no_real_secrets_in_env_example():
    text = (ROOT / ".env.example").read_text()
    assert "sk-ant-" not in text.replace("sk-ant-...", "")


def test_domain_folders_hold_their_files():
    pkg = ROOT / "src/sql_agent/domains"
    for name in ("catalog.json", "glossary.json", "seed.py", "planner.py", "rules.py", "warehouse.py", "examples.json", "demo.py"):
        assert (pkg / "retail" / name).is_file(), name
    for name in ("demo_banking_schema.sql", "demo_banking.example.json"):
        assert (pkg / "banking" / name).is_file(), name
    assert not (ROOT / "data/sample").exists() and not (ROOT / "examples/datasets").exists()


def test_cli_check_command_is_named_viewer(capsys):
    import pytest

    from sql_agent.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "viewer" in out and "doctor" not in out


def test_viewer_warns_about_the_old_sample_data_setting(monkeypatch, capsys):
    from sql_agent.cli import _viewer
    from sql_agent.config import get_settings

    monkeypatch.setenv("SAMPLE_DATA", "off")
    monkeypatch.delenv("RETAIL_DATA", raising=False)
    _viewer(get_settings())
    assert "rename it to RETAIL_DATA" in capsys.readouterr().out
