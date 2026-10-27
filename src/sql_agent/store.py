"""Service-role records: audit log, export requests, glossary proposals.

All writes here go through the SERVICE connection. None of these tables
is visible to the agent role, so model-written SQL can neither read the
audit trail nor approve its own export.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .executor import Executor, to_json_value
from .schemas import AskResult, User


class ApprovalError(Exception):
    pass


class Store:
    def __init__(self, executor: Executor):
        self.ex = executor

    # -- audit -------------------------------------------------------------------
    def write_audit(self, result: AskResult) -> int:
        u = result.user
        with self.ex.service() as svc:
            row = svc.execute(
                "INSERT INTO private.audit_log (user_id, role, region, pii_tier, question, kind, reason_code, reason, sql,"
                " attempts, rules_fired, row_count, truncated, scope, backend, duration_ms, output_scan)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING audit_id",
                (
                    u.user_id, u.role, u.region, u.pii_tier, result.question, result.kind, result.reason_code,
                    result.message[:500] if result.kind != "answer" else None,
                    result.sql, result.attempts, Jsonb([h.model_dump() for h in result.rules_fired]),
                    result.row_count if result.kind == "answer" else None, result.truncated, result.scope,
                    result.backend, result.duration_ms, result.output_scan,
                ),
            ).fetchone()
        return int(row[0])

    def audit(self, limit: int = 100, user_id: str | None = None) -> list[dict]:
        with self.ex.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            if user_id:
                cur.execute("SELECT * FROM private.audit_log WHERE user_id = %s ORDER BY audit_id DESC LIMIT %s", (user_id, limit))
            else:
                cur.execute("SELECT * FROM private.audit_log ORDER BY audit_id DESC LIMIT %s", (limit,))
            return [{k: to_json_value(v) for k, v in r.items()} for r in cur.fetchall()]

    # -- exports -------------------------------------------------------------------
    def create_export(self, user: User, question: str, sql: str, reason: str) -> dict:
        request_id = "EXP-" + secrets.token_hex(4).upper()
        with self.ex.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO private.export_requests (request_id, requested_by, question, sql, reason)"
                " VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (request_id, user.user_id, question, sql, reason),
            )
            return {k: to_json_value(v) for k, v in cur.fetchone().items()}

    def get_export(self, request_id: str) -> dict | None:
        with self.ex.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM private.export_requests WHERE request_id = %s", (request_id,))
            row = cur.fetchone()
            return {k: to_json_value(v) for k, v in row.items()} if row else None

    def list_exports(self, status: str | None = None) -> list[dict]:
        with self.ex.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            if status:
                cur.execute("SELECT * FROM private.export_requests WHERE status = %s ORDER BY created_at DESC", (status,))
            else:
                cur.execute("SELECT * FROM private.export_requests ORDER BY created_at DESC LIMIT 100")
            return [{k: to_json_value(v) for k, v in r.items()} for r in cur.fetchall()]

    def decide_export(self, request_id: str, approver: User, approve: bool, reason: str) -> dict:
        if not approver.can_approve:
            raise ApprovalError(f"{approver.user_id} is not an approver.")
        with self.ex.service() as svc, svc.transaction(), svc.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM private.export_requests WHERE request_id = %s FOR UPDATE", (request_id,))
            row = cur.fetchone()
            if row is None:
                raise ApprovalError("No such export request.")
            if row["status"] != "pending":
                raise ApprovalError(f"Already {row['status']}.")
            if row["requested_by"] == approver.user_id:
                raise ApprovalError("You cannot approve your own export request.")
            cur.execute(
                "UPDATE private.export_requests SET status = %s, decided_by = %s, decision_reason = %s, decided_at = now()"
                " WHERE request_id = %s RETURNING *",
                ("approved" if approve else "rejected", approver.user_id, reason, request_id),
            )
            return {k: to_json_value(v) for k, v in cur.fetchone().items()}

    # -- glossary proposals -----------------------------------------------------------
    def propose_term(self, user: User, term: str, definition: str, sql_hint: str) -> dict:
        proposal_id = "GLO-" + secrets.token_hex(4).upper()
        with self.ex.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO private.glossary_proposals (proposal_id, term, definition, sql_hint, proposed_by)"
                " VALUES (%s,%s,%s,%s,%s) RETURNING *",
                (proposal_id, term.strip().lower(), definition, sql_hint, user.user_id),
            )
            return {k: to_json_value(v) for k, v in cur.fetchone().items()}

    def list_proposals(self, status: str | None = None) -> list[dict]:
        with self.ex.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            if status:
                cur.execute("SELECT * FROM private.glossary_proposals WHERE status = %s ORDER BY created_at", (status,))
            else:
                cur.execute("SELECT * FROM private.glossary_proposals ORDER BY created_at DESC LIMIT 100")
            return [{k: to_json_value(v) for k, v in r.items()} for r in cur.fetchall()]

    def decide_proposal(self, proposal_id: str, approver: User, approve: bool, reason: str) -> dict:
        if not approver.can_approve:
            raise ApprovalError(f"{approver.user_id} is not an approver.")
        with self.ex.service() as svc, svc.transaction(), svc.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM private.glossary_proposals WHERE proposal_id = %s FOR UPDATE", (proposal_id,))
            row = cur.fetchone()
            if row is None:
                raise ApprovalError("No such proposal.")
            if row["status"] != "pending":
                raise ApprovalError(f"Already {row['status']}.")
            if row["proposed_by"] == approver.user_id:
                raise ApprovalError("You cannot approve your own proposal.")
            cur.execute(
                "UPDATE private.glossary_proposals SET status = %s, decided_by = %s, decision_reason = %s, decided_at = now()"
                " WHERE proposal_id = %s RETURNING *",
                ("approved" if approve else "rejected", approver.user_id, reason, proposal_id),
            )
            return {k: to_json_value(v) for k, v in cur.fetchone().items()}

    def approved_terms(self) -> list[dict[str, Any]]:
        try:
            return [p for p in self.list_proposals("approved")]
        except psycopg.Error:
            return []


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=to_json_value)
