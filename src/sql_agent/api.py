"""HTTP API + the web console.

Identity: in this prototype the ``X-Demo-User`` header picks one of the
synthetic users (``DEMO_IDENTITY=on``, the default). That is a demo
switch, not authentication — a production deployment replaces
``current_user`` with SSO / JWT verification and maps claims to
``private.app_users``. Nothing in the question body can change identity.

``API_KEY`` (optional) gates every ``/api`` route with ``X-API-Key``.
"""

from __future__ import annotations

import hmac
from functools import lru_cache
from importlib import resources

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from . import datasets, focus, knowledge
from .agent import Agent, UnknownUser
from .config import get_settings
from .schemas import AskRequest, AskResult, Decision, ExportRequestIn, GlossaryProposalIn, User
from .store import ApprovalError

app = FastAPI(title="Trida AI Blueprint — Governed Text-to-SQL", version="0.1.0")


@lru_cache(maxsize=1)
def get_agent() -> Agent:
    return Agent(get_settings())


def _static(name: str):
    return resources.files("sql_agent") / "static" / name


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    key = get_settings().api_key
    if key and not (x_api_key and hmac.compare_digest(x_api_key, key)):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key.")


def current_user(x_demo_user: str | None = Header(default=None), _: None = Depends(require_key)) -> User:
    settings = get_settings()
    if not settings.demo_identity:
        raise HTTPException(status_code=401, detail="No identity provider is configured (DEMO_IDENTITY is off).")
    if not x_demo_user:
        raise HTTPException(status_code=401, detail="Send X-Demo-User with one of the demo user ids (GET /api/users).")
    try:
        return get_agent().user(x_demo_user)
    except UnknownUser:
        raise HTTPException(status_code=401, detail=f"Unknown user {x_demo_user!r}.") from None


def data_selection(x_data: str | None = Header(default=None)) -> str:
    """X-Data: all | retail | <dataset schema> — narrows the catalog for this request (see focus.py)."""
    try:
        return focus.normalize(x_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/", response_class=HTMLResponse)
def console() -> HTMLResponse:
    # no-store: the page and the running server must always match (a cached page against
    # a newer server, or a new page against a server not yet restarted, mislead silently)
    return HTMLResponse(_static("index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/favicon.svg")
def favicon():
    return FileResponse(str(_static("brand/favicon.svg")), media_type="image/svg+xml")


@app.get("/brand/{name}")
def brand(name: str):
    if name not in {"tridaai-logo.png", "favicon.svg"}:
        raise HTTPException(status_code=404)
    return FileResponse(str(_static(f"brand/{name}")))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/readiness")
def readiness() -> dict:
    try:
        from .model_backends import check_backend

        agent = get_agent()
        info = agent.executor.ping()
        return {"status": "ready", "backend": agent.backend.name, "model_check": check_backend(agent.backend), **info}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"not ready: {type(e).__name__}: {e}") from None


@app.get("/api/users", dependencies=[Depends(require_key)])
def users() -> list[dict]:
    if not get_settings().demo_identity:
        raise HTTPException(status_code=404)
    return [u.model_dump() for u in get_agent().users().values()]


@app.get("/api/me")
def me(user: User = Depends(current_user), data: str = Depends(data_selection)) -> dict:
    agent = get_agent()
    from .screens import scope_line

    with focus.use(data):
        return {"data": data, "data_options": focus.options(), "data_default": focus.default(), "user": user.model_dump(), "scope": scope_line(user),
                "backend": agent.backend.name, "as_of": agent.settings.as_of_date.isoformat(), "max_rows": agent.settings.max_result_rows,
                "retail_data": knowledge.retail_enabled(), "views": len(knowledge.views_for(user)),
                "examples": (knowledge.retail_examples() if knowledge.retail_enabled() else None)
                if data in (focus.ALL, focus.RETAIL) else datasets.example_questions(
                    data, knowledge.catalog_for(user), agent.settings.as_of_date.year)}


@app.get("/api/catalog")
def catalog(user: User = Depends(current_user), data: str = Depends(data_selection)) -> dict:
    with focus.use(data):
        return knowledge.catalog_for(user)


@app.get("/api/glossary", dependencies=[Depends(require_key)])
def glossary(data: str = Depends(data_selection)) -> dict:
    with focus.use(data):
        return {"terms": knowledge.glossary(), "approved_proposals": get_agent().store.approved_terms()}


@app.post("/api/ask", response_model=AskResult)
def ask(body: AskRequest, user: User = Depends(current_user), data: str = Depends(data_selection)) -> AskResult:
    return get_agent().ask(user, body.question, data=data)


@app.post("/api/compare", response_model=list[AskResult])
def compare(body: AskRequest, user: User = Depends(current_user), data: str = Depends(data_selection)) -> list[AskResult]:
    """Demo only: the same question asked as every demo user, to show RLS + masking side by side."""
    agent = get_agent()
    return [agent.ask(u, body.question, data=data) for u in agent.users().values()]


@app.get("/api/audit")
def audit(limit: int = 50, user: User = Depends(current_user)) -> list[dict]:
    store = get_agent().store
    if user.role == "privacy_officer":
        return store.audit(limit=min(limit, 500))
    return store.audit(limit=min(limit, 100), user_id=user.user_id)


@app.post("/api/exports")
def request_export(body: ExportRequestIn, user: User = Depends(current_user), data: str = Depends(data_selection)) -> dict:
    try:
        return get_agent().request_export(user, body.question, body.reason, data=data)
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/exports")
def list_exports(status: str | None = None, user: User = Depends(current_user)) -> list[dict]:
    items = get_agent().store.list_exports(status)
    return items if user.can_approve else [i for i in items if i["requested_by"] == user.user_id]


@app.post("/api/exports/{request_id}/decision")
def decide_export(request_id: str, body: Decision, user: User = Depends(current_user)) -> dict:
    try:
        return get_agent().store.decide_export(request_id, user, body.approve, body.reason)
    except ApprovalError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None


@app.get("/api/exports/{request_id}/csv", response_class=PlainTextResponse)
def export_csv(request_id: str, user: User = Depends(current_user)) -> PlainTextResponse:
    try:
        text, n = get_agent().run_export(request_id, user)
    except ApprovalError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    return PlainTextResponse(text, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{request_id}.csv"', "X-Row-Count": str(n)})


@app.post("/api/glossary/proposals")
def propose(body: GlossaryProposalIn, user: User = Depends(current_user)) -> dict:
    return get_agent().store.propose_term(user, body.term, body.definition, body.sql_hint)


@app.get("/api/glossary/proposals")
def proposals(status: str | None = None, user: User = Depends(current_user)) -> list[dict]:
    return get_agent().store.list_proposals(status)


@app.post("/api/glossary/proposals/{proposal_id}/decision")
def decide_proposal(proposal_id: str, body: Decision, user: User = Depends(current_user)) -> dict:
    try:
        return get_agent().store.decide_proposal(proposal_id, user, body.approve, body.reason)
    except ApprovalError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None


@app.exception_handler(RuntimeError)
async def runtime_error(_: Request, exc: RuntimeError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=503, content={"detail": str(exc)})
