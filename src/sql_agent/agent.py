"""The pipeline: one question, one authenticated user, one audited answer.

    identity → period → screens → ground → [plan → validate → execute] ×≤3
             → compose → output scan → scope → audit

Only ``plan`` and ``compose`` involve a model. Identity comes from the
session; the period is resolved in code; screens, validation, the row
filter, masking, the output scan and the audit row are all code or
database — a manipulated model can change the SQL, but not what the
database returns or what reaches the user.
"""

from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass

from . import focus, knowledge, periods, screens
from .config import Settings, get_settings
from .executor import ExecutionError, Executor
from .model_backends import MockBackend, ModelBackend, build_backend, describe_model_error
from .output_scan import scan_rows, scan_text
from .planner import Plan
from .schemas import AskResult, ClarifyOption, RuleHit, Stage, User
from .store import ApprovalError, Store
from .validator import ValidationResult, validate


class UnknownUser(Exception):
    pass


@dataclass
class _Clock:
    t: float

    def lap(self) -> int:
        now = time.perf_counter()
        ms = int((now - self.t) * 1000)
        self.t = now
        return ms


class Agent:
    def __init__(self, settings: Settings | None = None, *, executor: Executor | None = None,
                 backend: ModelBackend | None = None, store: Store | None = None):
        self.settings = settings or get_settings()
        self.executor = executor or Executor(self.settings)
        self.backend = backend or build_backend(self.settings)
        self.store = store or Store(self.executor)
        self._users: dict[str, User] | None = None

    # -- identity ------------------------------------------------------------------
    def users(self, refresh: bool = False) -> dict[str, User]:
        if self._users is None or refresh:
            demo = _demo_users()
            about = {u["user_id"]: u.get("about") for u in demo}
            order = {u["user_id"]: i for i, u in enumerate(demo)}
            rows = sorted(self.executor.load_users(), key=lambda r: (order.get(r["user_id"], 999), r["user_id"]))
            self._users = {r["user_id"]: User(**r, about=about.get(r["user_id"])) for r in rows}
        return self._users

    def user(self, user_id: str) -> User:
        u = self.users().get(user_id)
        if u is None:
            raise UnknownUser(user_id)
        return u

    # -- the question ----------------------------------------------------------------
    def ask(self, user: User, question: str, *, audit: bool = True, data: str | None = None) -> AskResult:
        """Answer one question. ``data`` narrows it to the retail data or one dataset (see focus.py)."""
        with focus.use(data):
            return self._ask(user, question, audit=audit)

    def _ask(self, user: User, question: str, *, audit: bool) -> AskResult:
        started = time.perf_counter()
        clock = _Clock(started)
        question = question.strip()
        stages: list[Stage] = [Stage(name="identity", status="ok", ms=0,
                                     detail=f"{user.user_id} · {user.role}" + (f" · {user.region}" if user.region else "") + f" · PII {user.pii_tier}")]
        result = AskResult(question=question, user=user, kind="error", message="", scope=screens.scope_line(user),
                           backend=self.backend.name)

        period = periods.resolve(question, self.settings.as_of_date, self.settings.fiscal_year_start_month)
        if isinstance(period, periods.Period):
            result.period = period.label
        stages.append(Stage(name="period", status="ok", ms=clock.lap(),
                            detail=period.describe() if isinstance(period, periods.Period)
                            else ("ambiguous: " + period.phrase if period else "none mentioned")))

        stop, trace = screens.run_screens(question, user, period)
        stages.append(Stage(name="screens", status="fail" if stop else "ok", ms=clock.lap(),
                            detail=", ".join(f"{n}={r}" for n, r in trace)))
        if stop:
            result.kind = stop.kind  # type: ignore[assignment]
            result.reason_code = stop.reason_code
            result.message = stop.message
            result.options = stop.options
            return self._finish(result, stages, started, audit)

        views = knowledge.views_for(user)
        stages.append(Stage(name="ground", status="ok", ms=clock.lap(),
                            detail=(f"data={focus.current()} · " if focus.current() != focus.ALL else "")
                            + f"{len(views)} views: {', '.join(views)}"))
        if not views:  # nothing this role may read: say so plainly instead of asking the model
            result.kind, result.reason_code = "decline", "no_access"
            where = "" if focus.current() == focus.ALL else f" in the '{focus.current()}' data selection"
            result.message = (f"Your role ({user.role.replace('_', ' ')}) has no access to any data views{where}, so there is "
                              "nothing I can query for you. An administrator grants access per role in the dataset spec "
                              "(the \"rows\" rules in datasets/<schema>.json), then runs `sql-agent dataset apply`.")
            return self._finish(result, stages, started, audit)

        extra_terms = self.store.approved_terms() if not isinstance(self.backend, MockBackend) else []
        plan: Plan | None = None
        validation: ValidationResult | None = None
        executed = None
        feedback: str | None = None
        previous_sql: str | None = None
        for attempt in range(1, self.settings.max_sql_attempts + 1):
            result.attempts = attempt
            try:
                plan = self.backend.plan(question, user, period if isinstance(period, periods.Period) else None,
                                         self.settings.as_of_date, feedback=feedback, previous_sql=previous_sql,
                                         extra_terms=extra_terms)
            except Exception as e:  # provider failure: report, do not guess
                why = describe_model_error(e)
                stages.append(Stage(name=f"plan#{attempt}", status="fail", ms=clock.lap(), detail=why[:300]))
                result.kind, result.reason_code = "error", "model_error"
                result.message = f"The model backend failed, so I have no answer and nothing was run against the data. {why}"
                return self._finish(result, stages, started, audit)
            if plan.action in {"clarify", "decline"}:
                stages.append(Stage(name=f"plan#{attempt}", status="ok", ms=clock.lap(), detail=plan.action))
                result.kind = plan.action  # type: ignore[assignment]
                result.reason_code = plan.reason_code
                result.message = plan.message
                result.options = [ClarifyOption(**o) for o in plan.options]
                return self._finish(result, stages, started, audit)
            stages.append(Stage(name=f"plan#{attempt}", status="ok" if plan.sql else "fail", ms=clock.lap(),
                                detail=(plan.sql or plan.message or "no SQL")[:160]))
            if not plan.sql:
                result.rules_fired.append(RuleHit(rule="no_sql", message=plan.message or "No SQL returned.", attempt=attempt))
                feedback, previous_sql = "- no_sql: reply with the JSON object containing one SELECT.", None
                continue
            validation = validate(plan.sql, user, max_rows=self.settings.max_result_rows)
            if not validation.ok:
                for h in validation.hits:
                    result.rules_fired.append(RuleHit(rule=h.rule, message=h.message, attempt=attempt))
                stages.append(Stage(name=f"validate#{attempt}", status="fail", ms=clock.lap(),
                                    detail=", ".join(sorted({h.rule for h in validation.hits}))))
                feedback, previous_sql = validation.feedback(), plan.sql
                continue
            stages.append(Stage(name=f"validate#{attempt}", status="ok", ms=clock.lap(),
                                detail="views: " + ", ".join(validation.tables)))
            try:
                executed = self.executor.run(user.user_id, validation.sql, max_rows=self.settings.max_result_rows)
            except ExecutionError as e:
                result.rules_fired.append(RuleHit(rule="execution_error", message=str(e)[:300], attempt=attempt))
                stages.append(Stage(name=f"execute#{attempt}", status="fail", ms=clock.lap(), detail=str(e)[:160]))
                feedback, previous_sql = f"- execution_error: {e}", plan.sql
                continue
            stages.append(Stage(name=f"execute#{attempt}", status="ok", ms=clock.lap(),
                                detail=f"{len(executed.rows)} rows{' (truncated)' if executed.truncated else ''} · read-only · RLS as {user.user_id}"))
            break

        if executed is None or plan is None or validation is None or not validation.ok:
            result.kind, result.reason_code = "decline", "no_safe_query"
            last = result.rules_fired[-1].message if result.rules_fired else "no SQL was produced"
            result.message = (f"I couldn't produce a query that passes the safety checks after {result.attempts} attempts "
                              f"(last problem: {last}). Nothing outside the checks was run. Try rephrasing, or ask a narrower question.")
            result.sql = validation.sql if validation and validation.ok else (plan.sql if plan else None)
            return self._finish(result, stages, started, audit)

        result.sql = validation.sql
        result.columns = executed.columns
        result.rows = [list(r) for r in executed.rows]
        result.row_count = len(executed.rows)
        result.truncated = executed.truncated
        result.definitions_used = plan.definitions_used

        try:
            message = self.backend.compose(question, user, plan, result.columns, result.rows, result.period, result.truncated)
        except Exception:
            message = MockBackend().compose(question, user, plan, result.columns, result.rows, result.period, result.truncated)
        compose_ms = clock.lap()

        scan_status = "clean"
        if user.pii_tier != "full":
            text_scan = scan_text(message)
            row_scan, bad_cols = scan_rows(result.columns, result.rows)
            if not text_scan.clean:
                message = MockBackend().compose(question, user, Plan("sql", presentation={}), result.columns, result.rows,
                                                result.period, result.truncated)
                scan_status = "blocked_text:" + ",".join(text_scan.findings)
            if not row_scan.clean:
                for r in result.rows:
                    for i in bad_cols:
                        r[i] = "[WITHHELD]"
                scan_status = ("blocked_cells:" if scan_status == "clean" else scan_status + ";cells:") + ",".join(row_scan.findings)
        else:
            scan_status = "skipped_full_tier"
        result.output_scan = scan_status
        stages.append(Stage(name="compose", status="ok", ms=compose_ms, detail=self.backend.name))
        stages.append(Stage(name="output_scan", status="ok" if scan_status in {"clean", "skipped_full_tier"} else "warn",
                            ms=clock.lap(), detail=scan_status))

        note = screens.scope_note(question, user)
        result.scope_note = note
        if result.truncated:
            result.export_available = True
            message += (f" Showing the first {self.settings.max_result_rows:,} rows; the full result needs an export, "
                        "which an approver must sign off.")
        result.message = message
        result.kind = "answer"
        return self._finish(result, stages, started, audit)

    def _finish(self, result: AskResult, stages: list[Stage], started: float, audit: bool) -> AskResult:
        result.stages = stages
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if audit:
            try:
                result.audit_id = self.store.write_audit(result)
                stages.append(Stage(name="audit", status="ok", ms=0, detail=f"audit #{result.audit_id}"))
            except Exception as e:  # the answer stands; the failure is visible
                stages.append(Stage(name="audit", status="fail", ms=0, detail=f"{type(e).__name__}"))
        return result

    # -- exports (human gate) ------------------------------------------------------------
    def request_export(self, user: User, question: str, reason: str, *, data: str | None = None) -> dict:
        with focus.use(data):
            return self._request_export(user, question, reason)

    def _request_export(self, user: User, question: str, reason: str) -> dict:
        period = periods.resolve(question, self.settings.as_of_date, self.settings.fiscal_year_start_month)
        stop, _ = screens.run_screens(question, user, period)
        if stop:
            raise ApprovalError(stop.message)
        plan = self.backend.plan(question, user, period if isinstance(period, periods.Period) else None, self.settings.as_of_date)
        if plan.action != "sql" or not plan.sql:
            raise ApprovalError(plan.message or "No query could be planned for this export.")
        v = validate(plan.sql, user, max_rows=self.settings.export_max_rows)
        if not v.ok:
            raise ApprovalError("The export query failed validation: " + v.feedback())
        return self.store.create_export(user, question, v.sql, reason)

    def run_export(self, request_id: str, viewer: User) -> tuple[str, int]:
        req = self.store.get_export(request_id)
        if req is None:
            raise ApprovalError("No such export request.")
        if req["status"] != "approved":
            raise ApprovalError(f"Export is {req['status']}, not approved.")
        if viewer.user_id != req["requested_by"]:
            raise ApprovalError("Only the requester can download an approved export.")
        requester = self.user(req["requested_by"])
        v = validate(req["sql"], requester, max_rows=self.settings.export_max_rows)  # re-checked at run time
        if not v.ok:
            raise ApprovalError("The stored export query no longer passes validation.")
        res = self.executor.run(requester.user_id, v.sql, max_rows=self.settings.export_max_rows)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(res.columns)
        w.writerows(res.rows)
        return buf.getvalue(), len(res.rows)


def _demo_users() -> list[dict]:
    from .migrations import load_users

    return load_users()
