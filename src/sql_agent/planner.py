"""The plan a model backend returns, and the offline planner entry point.

``Plan`` is the contract between any backend (Ollama, Anthropic, OpenAI-compatible
or the offline planner) and the agent. ``plan()`` is the offline planner used by
``MODEL_BACKEND=mock``: a deterministic stand-in that only knows the retail data's
question shapes — it lives in ``domains/retail/planner.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .periods import Period
    from .schemas import User

@dataclass
class Plan:
    action: str  # "sql" | "clarify" | "decline"
    sql: str | None = None
    message: str = ""
    reason_code: str | None = None
    definitions_used: list[str] = field(default_factory=list)
    presentation: dict = field(default_factory=dict)
    options: list[dict] = field(default_factory=list)


def plan(question: str, user: "User", period: "Period | None", as_of: date) -> Plan:
    """The offline planner (retail data only)."""
    from .domains.retail.planner import plan as retail_plan

    return retail_plan(question, user, period, as_of)
