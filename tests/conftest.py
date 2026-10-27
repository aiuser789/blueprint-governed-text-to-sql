"""Shared fixtures.

Tests marked ``db`` need PostgreSQL (``DATABASE_URL``); they are skipped
without it, so the hermetic suite (validator, screens, periods, planner,
scanner, backends) runs anywhere.
"""

from __future__ import annotations

import os

import pytest

from sql_agent.config import get_settings
from sql_agent.schemas import User

USERS = {
    "priya": User(user_id="priya", display_name="Priya Raman", role="supervisor_officer", region="West", pii_tier="masked"),
    "marco": User(user_id="marco", display_name="Marco Silva", role="supervisor_officer", region="East", pii_tier="masked"),
    "dev": User(user_id="dev", display_name="Dev Patel", role="sales_rep", region="East", rep_id="R-E-03", pii_tier="partial"),
    "sam": User(user_id="sam", display_name="Sam Okafor", role="analyst", pii_tier="masked", can_approve=True),
    "lena": User(user_id="lena", display_name="Lena Fischer", role="privacy_officer", pii_tier="full", can_approve=True),
    "tom": User(user_id="tom", display_name="Tom Becker", role="contractor", pii_tier="masked"),
}


@pytest.fixture
def users() -> dict[str, User]:
    return USERS


def pytest_collection_modifyitems(config, items):
    if os.environ.get("DATABASE_URL") or get_settings().database_url:
        return
    skip = pytest.mark.skip(reason="needs PostgreSQL: set DATABASE_URL (see README)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs PostgreSQL (DATABASE_URL)")


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def migrated(settings):
    from sql_agent.migrations import migrate

    migrate(settings)
    return settings


@pytest.fixture(scope="session")
def agent(migrated):
    from sql_agent.agent import Agent
    from sql_agent.model_backends import MockBackend

    return Agent(migrated, backend=MockBackend())
