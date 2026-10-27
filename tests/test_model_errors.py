"""A failing model is reported clearly, and nothing runs against the data."""

from __future__ import annotations

import pytest

from sql_agent.agent import Agent
from sql_agent.model_backends import ModelBackendError, describe_model_error
from sql_agent.model_backends import MockBackend


class AuthenticationError(Exception):  # same class name the provider SDKs raise
    pass


def test_describe_known_errors():
    assert "API key was rejected" in describe_model_error(AuthenticationError("401 invalid x-api-key"))
    assert describe_model_error(ModelBackendError("Run: ollama pull x")) == "Run: ollama pull x"
    assert describe_model_error(ValueError("x")).startswith("ValueError")


class Broken(MockBackend):
    name = "broken"

    def __init__(self, exc):
        self.exc = exc

    def plan(self, *a, **kw):
        raise self.exc


@pytest.mark.db
def test_agent_surfaces_the_reason(migrated):
    agent = Agent(migrated, backend=Broken(ModelBackendError("Ollama is not reachable at http://localhost:11434. Start it with `ollama serve`.")))
    r = agent.ask(agent.user("priya"), "total revenue in the last 2 months")
    assert r.kind == "error" and r.reason_code == "model_error"
    assert "ollama serve" in r.message and "nothing was run" in r.message
    assert r.sql is None and not r.rows
