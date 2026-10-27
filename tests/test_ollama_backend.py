"""The Ollama backend against a stand-in server that speaks Ollama's HTTP API.

Hermetic: a real HTTP server on 127.0.0.1 implements /api/chat and
/api/tags the way Ollama does, so these tests exercise the actual
request/response path (URL, JSON body, options, errors) with no model.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sql_agent.config import get_settings
from sql_agent.model_backends import OllamaBackend, build_backend, check_backend

AS_OF = date(2026, 9, 30)


class FakeOllama:
    def __init__(self, models=("qwen2.5-coder:7b",), reply='{"action": "sql", "sql": "SELECT COUNT(*) AS n FROM orders"}'):
        self.models = list(models)
        self.reply = reply
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m, "model": m} for m in fake.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if self.path != "/api/chat":
                    return self._send(404, {"error": "not found"})
                if body["model"] not in fake.models:
                    return self._send(404, {"error": f"model '{body['model']}' not found, try pulling it first"})
                self._send(200, {"model": body["model"], "message": {"role": "assistant", "content": fake.reply}, "done": True})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = FakeOllama()
    yield f
    f.close()


@pytest.fixture
def env(monkeypatch, fake):
    monkeypatch.setenv("OLLAMA_BASE_URL", fake.url)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    return fake


def test_build_backend_selects_ollama(monkeypatch, env):
    monkeypatch.setenv("MODEL_BACKEND", "ollama")
    b = build_backend(get_settings())
    assert isinstance(b, OllamaBackend) and b.name == "ollama:qwen2.5-coder:7b"


def test_plan_request_shape(env, users):
    p = OllamaBackend(get_settings()).plan("how many orders all time", users["dev"], None, AS_OF)
    assert p.action == "sql" and p.sql == "SELECT COUNT(*) AS n FROM orders"
    req = env.requests[-1]
    assert req["model"] == "qwen2.5-coder:7b" and req["stream"] is False and req["format"] == "json"
    assert req["options"]["temperature"] == 0 and req["options"]["num_ctx"] == 8192
    system, user = req["messages"][0]["content"], req["messages"][1]["content"]
    assert "read-only PostgreSQL SELECT" in system
    assert "view customers" in user and "region_month_summary" not in user  # role-trimmed: sales reps get no summaries
    assert "role=sales_rep" in user
    assert "core." not in user and "private." not in user


def test_compose_is_plain_text_and_strips_reasoning(env, users):
    env.reply = "<think>sum it up</think>Revenue in August 2026 was $131,585.85."
    from sql_agent.planner import Plan

    text = OllamaBackend(get_settings()).compose("q", users["sam"], Plan("sql"), ["revenue"], [[131585.85]], "August 2026", False)
    assert text == "Revenue in August 2026 was $131,585.85."
    assert "format" not in env.requests[-1]


def test_v1_suffix_tolerated(monkeypatch, env, users):
    monkeypatch.setenv("OLLAMA_BASE_URL", env.url + "/v1")
    b = OllamaBackend(get_settings())
    assert b.base_url == env.url
    assert b.plan("x", users["sam"], None, AS_OF).sql


def test_model_not_pulled(monkeypatch, env, users):
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    b = OllamaBackend(get_settings())
    with pytest.raises(Exception) as err:
        b.plan("x", users["sam"], None, AS_OF)
    assert "ollama pull llama3.1:8b" in str(err.value)
    check = check_backend(b)
    assert not check["ok"] and "ollama pull llama3.1:8b" in check["detail"]


def test_ollama_not_running(monkeypatch, users):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens on the discard port
    b = OllamaBackend(get_settings())
    with pytest.raises(Exception) as err:
        b.plan("x", users["sam"], None, AS_OF)
    assert "ollama serve" in str(err.value)
    assert not check_backend(b)["ok"]


def test_check_ok(env):
    assert check_backend(OllamaBackend(get_settings()))["ok"]


def test_ollama_default_timeout_allows_model_loading(monkeypatch, env):
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    assert OllamaBackend(get_settings()).timeout == 300
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45")
    assert OllamaBackend(get_settings()).timeout == 45
