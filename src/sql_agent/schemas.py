"""Typed records at the boundaries (API, CLI, audit, evals)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["supervisor_officer", "sales_rep", "analyst", "privacy_officer", "contractor"]
Tier = Literal["full", "partial", "masked"]
Kind = Literal["answer", "clarify", "decline", "error"]


class User(BaseModel):
    """An authenticated identity. Comes from the session, never from the question."""

    user_id: str
    display_name: str
    role: Role
    region: str | None = None
    rep_id: str | None = None
    pii_tier: Tier
    can_approve: bool = False
    about: str | None = None


class Stage(BaseModel):
    name: str
    status: Literal["ok", "fail", "skip", "warn"]
    ms: int = 0
    detail: str = ""


class RuleHit(BaseModel):
    rule: str
    message: str
    attempt: int = 1


class ClarifyOption(BaseModel):
    key: str
    label: str
    rewrite: str | None = None


class AskResult(BaseModel):
    question: str
    user: User
    kind: Kind
    message: str
    reason_code: str | None = None
    options: list[ClarifyOption] = Field(default_factory=list)
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    scope: str = ""
    scope_note: str | None = None
    period: str | None = None
    definitions_used: list[str] = Field(default_factory=list)
    attempts: int = 0
    rules_fired: list[RuleHit] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)
    backend: str = "mock"
    output_scan: str = "not_run"
    export_available: bool = False
    audit_id: int | None = None
    duration_ms: int = 0


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ExportRequestIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=3, max_length=500)


class Decision(BaseModel):
    approve: bool
    reason: str = Field(min_length=3, max_length=500)


class GlossaryProposalIn(BaseModel):
    term: str = Field(min_length=2, max_length=80)
    definition: str = Field(min_length=10, max_length=1000)
    sql_hint: str = Field(min_length=3, max_length=1000)
