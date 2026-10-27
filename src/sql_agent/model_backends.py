"""Model backends behind one small interface.

- :class:`MockBackend` — the offline planner (``domains/retail/planner.py``) plus a
  templated answer writer. Deterministic; no key; labelled everywhere.
- :class:`OllamaBackend` — a local model served by Ollama (the
  recommended profile): free, no API key, and the schema, questions and
  masked results never leave the machine. Talks to Ollama's HTTP API
  with the standard library, so it needs no extra package.
- :class:`AnthropicBackend` — Claude via the Anthropic SDK.
- :class:`OpenAIBackend` — any OpenAI-compatible endpoint (OpenAI,
  NVIDIA NIM, vLLM, gateways).

A backend does exactly two things: ``plan`` (question → SQL / clarify /
decline) and ``compose`` (masked result → a short answer). It never sees
raw PII, never executes anything, and its output is validated by code.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import date
from typing import Protocol

from . import knowledge, prompts
from .config import Settings
from .periods import Period
from .planner import Plan, plan as offline_plan
from .schemas import User
from .screens import scope_line


class ModelBackendError(RuntimeError):
    """A provider failure, with a message that is safe and useful to show the user."""


def describe_model_error(exc: Exception) -> str:
    """One line for the user: what failed and what to do. Never includes secrets."""
    if isinstance(exc, ModelBackendError):
        return str(exc)
    name = type(exc).__name__
    hints = {
        "AuthenticationError": "the API key was rejected — check the key in .env (and that no old key is exported in your shell)",
        "PermissionDeniedError": "the account is not allowed to use this model, or has no credits",
        "NotFoundError": "the model name was not found for this account — check the *_MODEL setting",
        "RateLimitError": "the provider is rate-limiting this account — wait and retry",
        "APIConnectionError": "the provider could not be reached — check the network or base URL",
        "APITimeoutError": "the provider timed out — retry, or raise LLM_TIMEOUT_SECONDS",
    }
    return f"{name}: {hints.get(name, 'see the server log for details')}"


def _strip_reasoning(text: str) -> str:
    """Local reasoning models (qwen3, deepseek-r1) prefix <think>…</think>; drop it."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


class ModelBackend(Protocol):
    name: str

    def plan(self, question: str, user: User, period: Period | None, as_of: date, *,
             feedback: str | None = None, previous_sql: str | None = None, extra_terms: list[dict] | None = None) -> Plan: ...

    def compose(self, question: str, user: User, plan: Plan, columns: list[str], rows: list[list],
                period_label: str | None, truncated: bool) -> str: ...


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _label(v) -> str:
    """'2026-08-01' (a month bucket) reads better as 'Aug 2026'."""
    if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v.endswith("-01") and v[:4].isdigit():
        return f"{_MON[int(v[5:7]) - 1]} {v[:4]}"
    return str(v)


def _num(v) -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


class MockBackend:
    name = "mock (offline planner)"

    def plan(self, question, user, period, as_of, *, feedback=None, previous_sql=None, extra_terms=None) -> Plan:
        return offline_plan(question, user, period, as_of)

    def compose(self, question, user, plan, columns, rows, period_label, truncated) -> str:
        p = plan.presentation or {}
        kind = p.get("kind")
        when = f" for {period_label}" if period_label else ""
        money = p.get("money")
        fmt = _money if money else _num
        if not rows:
            return f"No rows matched{when} within your access."
        if kind == "scalar":
            row = dict(zip(columns, rows[0]))
            value = row.get(p.get("value"), rows[0][0])
            if value is None:
                return f"No matching orders{when} within your access."
            text = f"{p.get('label', columns[0]).capitalize()}{when}: {fmt(value)}"
            if "orders" in row and p.get("value") != "orders":
                text += f" across {_num(row['orders'])} orders"
            return text + "."
        if kind == "ranking":
            name, value = p.get("name"), p.get("value")
            ni, vi = columns.index(name), columns.index(value)
            top = "; ".join(f"{i + 1}. {r[ni]} ({fmt(r[vi])})" for i, r in enumerate(rows[:3]))
            return f"{p.get('label')}{when}: {top}" + (f" — {len(rows)} in the table." if len(rows) > 3 else ".")
        if kind in {"breakdown", "series"}:
            x, value = p.get("x"), p.get("value")
            xi, vi = columns.index(x), columns.index(value)
            if kind == "series":
                best = max(rows, key=lambda r: (r[vi] or 0))
                return (f"{p.get('label', '').capitalize()}{when}: {len(rows)} periods, from {fmt(rows[0][vi])} in {_label(rows[0][xi])} "
                        f"to {fmt(rows[-1][vi])} in {_label(rows[-1][xi])}; highest {fmt(best[vi])} in {_label(best[xi])}.")
            if len(columns) > 2 and len({r[xi] for r in rows}) != len(rows):
                return f"{p.get('label', '').capitalize()}{when}: {len(rows)} rows (see the table)."
            parts = ", ".join(f"{_label(r[xi])} {fmt(r[vi])}" for r in rows[:6])
            return f"{p.get('label', '').capitalize()}{when}: {parts}" + (" …" if len(rows) > 6 else ".")
        if kind == "row":
            row = dict(zip(columns, rows[0]))
            return f"{p.get('label')}{when}: " + ", ".join(f"{k.replace('_', ' ')} {_num(v)}" for k, v in row.items()) + "."
        more = " (more than the display limit — request an export for the full set)" if truncated else ""
        return f"{len(rows)} rows{when}{more}."


class _LLMBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _complete(self, system: str, user: str, json_mode: bool = False) -> str:  # pragma: no cover - provider specific
        raise NotImplementedError

    def plan(self, question, user, period, as_of, *, feedback=None, previous_sql=None, extra_terms=None) -> Plan:
        fb = prompts.FEEDBACK.format(sql=previous_sql or "(none)", problems=feedback) if feedback else ""
        text = self._complete(
            prompts.PLAN_SYSTEM,
            prompts.PLAN_USER.format(
                role=user.role, scope=scope_line(user), as_of=as_of.isoformat(),
                period=period.describe() if period else "none mentioned",
                catalog=knowledge.render_catalog(user), glossary=knowledge.render_glossary(extra_terms),
                question=question, feedback=fb,
            ),
            json_mode=True,
        )
        return parse_plan(_strip_reasoning(text))

    def compose(self, question, user, plan, columns, rows, period_label, truncated) -> str:
        shown = rows[:50]
        text = self._complete(
            prompts.COMPOSE_SYSTEM,
            prompts.COMPOSE_USER.format(
                question=question, period=period_label or "not specified", columns=", ".join(columns),
                row_count=len(rows), truncated=", truncated" if truncated else "", shown=len(shown),
                rows="\n".join(json.dumps(r, default=str) for r in shown),
            ),
        )
        return _strip_reasoning(text)


def parse_plan(text: str) -> Plan:
    """Parse a model reply into a Plan; anything malformed is a decline."""
    obj = None
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            obj = None
    if not isinstance(obj, dict):
        return Plan("sql", sql=None, message="The model reply was not valid JSON.", reason_code="malformed")
    action = str(obj.get("action", "")).lower()
    if action == "sql":
        return Plan("sql", sql=str(obj.get("sql") or "").strip() or None,
                    definitions_used=[str(d) for d in obj.get("definitions_used") or []][:8],
                    message=str(obj.get("explanation") or ""))
    if action == "clarify":
        opts = [{"key": f"opt{i + 1}", "label": str(o)} for i, o in enumerate(obj.get("options") or [])][:4]
        return Plan("clarify", message=str(obj.get("message") or "Could you clarify the question?"), reason_code="model_clarify", options=opts)
    if action == "decline":
        return Plan("decline", message=str(obj.get("message") or "I can't answer that from the available data."),
                    reason_code=str(obj.get("reason_code") or "model_decline"))
    return Plan("sql", sql=None, message="The model reply had no recognised action.", reason_code="malformed")


class AnthropicBackend(_LLMBackend):
    def __init__(self, settings: Settings, client=None):
        super().__init__(settings)
        if client is None:
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not key:
                raise RuntimeError("MODEL_BACKEND=anthropic but ANTHROPIC_API_KEY is not set.")
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("MODEL_BACKEND=anthropic needs the 'anthropic' package: uv sync --extra llm") from e
            client = anthropic.Anthropic(api_key=key, timeout=settings.llm_timeout_seconds)
        self.client = client
        self.model = os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-5-5"
        self.name = f"anthropic:{self.model}"

    def _complete(self, system: str, user: str, json_mode: bool = False) -> str:
        msg = self.client.messages.create(
            model=self.model, max_tokens=self.settings.llm_max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in msg.content)


class OpenAIBackend(_LLMBackend):
    def __init__(self, settings: Settings, client=None):
        super().__init__(settings)
        self.model = os.environ.get("OPENAI_MODEL", "").strip()
        if client is None:
            if not self.model:
                raise RuntimeError("MODEL_BACKEND=openai needs OPENAI_MODEL (there is no default model name).")
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                raise RuntimeError("MODEL_BACKEND=openai but OPENAI_API_KEY is not set.")
            try:
                import openai
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("MODEL_BACKEND=openai needs the 'openai' package: uv sync --extra llm") from e
            client = openai.OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL") or None,
                                   timeout=settings.llm_timeout_seconds)
        self.client = client
        self.name = f"openai:{self.model or 'test'}"

    def _complete(self, system: str, user: str, json_mode: bool = False) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, max_tokens=self.settings.llm_max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


OLLAMA_DEFAULT_URL = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:7b"


class OllamaBackend(_LLMBackend):
    """A local model served by Ollama, via its native HTTP API (/api/chat).

    - ``OLLAMA_BASE_URL``  default http://localhost:11434 (a trailing /v1 is tolerated)
    - ``OLLAMA_MODEL``     default qwen2.5-coder:7b (pull it first: ``ollama pull …``)
    - ``OLLAMA_NUM_CTX``   context window, default 8192. Ollama's own default is
                           smaller than the catalog + glossary prompt, and a
                           truncated prompt fails silently, so it is always sent.

    Plans are requested with ``format: "json"`` (Ollama constrains the output
    to valid JSON) and temperature 0, so the same question gives the same SQL.
    """

    def __init__(self, settings: Settings, opener=None):
        super().__init__(settings)
        base = (os.environ.get("OLLAMA_BASE_URL", "").strip() or OLLAMA_DEFAULT_URL).rstrip("/")
        self.base_url = base[:-3] if base.endswith("/v1") else base
        self.model = os.environ.get("OLLAMA_MODEL", "").strip() or OLLAMA_DEFAULT_MODEL
        self.num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "").strip() or 8192)
        self.name = f"ollama:{self.model}"
        self._open = opener or urllib.request.urlopen
        # A local model's first call loads several GB into memory, which can take
        # minutes on a laptop. Default 300 s unless LLM_TIMEOUT_SECONDS is set.
        explicit = os.environ.get("LLM_TIMEOUT_SECONDS", "").strip()
        self.timeout = int(explicit) if explicit else 300

    def _request(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode() or "{}").get("error", "")
            except Exception:
                pass
            if e.code == 404 and "not found" in detail.lower():
                raise ModelBackendError(
                    f"Ollama does not have the model '{self.model}' yet. Download it with: ollama pull {self.model}") from None
            raise ModelBackendError(f"Ollama returned HTTP {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise ModelBackendError(
                f"Ollama is not reachable at {self.base_url} ({getattr(e, 'reason', e)}). "
                "Start it with `ollama serve` or open the Ollama app.") from None
        except TimeoutError:
            raise ModelBackendError(
                f"Ollama took longer than {timeout or self.timeout}s. The first call loads the model into memory: run "
                f"`ollama run {self.model} hi` once to load it, then retry; if every call is this slow, raise "
                "LLM_TIMEOUT_SECONDS or use a smaller model (e.g. qwen2.5-coder:3b).") from None

    def _complete(self, system: str, user: str, json_mode: bool = False) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "keep_alive": "15m",
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": self.settings.llm_max_tokens},
        }
        if json_mode:
            body["format"] = "json"
        reply = self._request("/api/chat", body)
        return (reply.get("message") or {}).get("content", "")

    def check(self) -> dict:
        """Is Ollama up, and is the configured model downloaded?"""
        try:
            tags = self._request("/api/tags", timeout=5)
        except ModelBackendError as e:
            return {"ok": False, "detail": str(e)}
        names = {m.get("name", "") for m in tags.get("models", [])} | {m.get("model", "") for m in tags.get("models", [])}
        wanted = {self.model, f"{self.model}:latest"}
        if names & wanted:
            return {"ok": True, "detail": f"{self.model} is available at {self.base_url}"}
        return {"ok": False, "detail": f"Ollama is running but '{self.model}' is not downloaded. Run: ollama pull {self.model}"}


def check_backend(backend: ModelBackend) -> dict:
    if isinstance(backend, MockBackend):
        return {"ok": True, "detail": "offline planner (no model needed)"}
    if isinstance(backend, OllamaBackend):
        return backend.check()
    return {"ok": True, "detail": "not checked (hosted provider; the first question tests it)"}


def build_backend(settings: Settings) -> ModelBackend:
    name = settings.model_backend
    if name in {"", "mock", "offline"}:
        return MockBackend()
    if name == "ollama":
        return OllamaBackend(settings)
    if name == "anthropic":
        return AnthropicBackend(settings)
    if name in {"openai", "nim", "vllm"}:
        return OpenAIBackend(settings)
    raise RuntimeError(f"Unknown MODEL_BACKEND={name!r} (use ollama, mock, anthropic or openai).")
