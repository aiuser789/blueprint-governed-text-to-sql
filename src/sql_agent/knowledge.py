"""The catalog and glossary the planner is grounded on, trimmed per role.

``catalog_for(user)`` returns only the analytics views that user's role
may query, with PII columns annotated with what that user's tier will
actually see. The model never learns that base tables, sessions or the
audit log exist.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from .schemas import User


@lru_cache(maxsize=1)
def _catalog() -> dict:
    return json.loads((resources.files("sql_agent.domains.retail") / "catalog.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _glossary() -> dict:
    return json.loads((resources.files("sql_agent.domains.retail") / "glossary.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def retail_examples() -> list[list[str]]:
    return json.loads((resources.files("sql_agent.domains.retail") / "examples.json").read_text(encoding="utf-8"))["examples"]


def retail_configured() -> bool:
    """RETAIL_DATA=off hides the retail data from the model, the validator
    and the question screens — for deployments that only query their own datasets."""
    import os

    return os.environ.get("RETAIL_DATA", "on").strip().lower() not in {"off", "0", "false", "no"}


def retail_enabled() -> bool:
    """The retail data is in play: configured on, and the current data selection includes it."""
    from . import focus

    key = focus.current()
    return key == focus.RETAIL or (key == focus.ALL and retail_configured())


def all_views() -> dict:
    """The retail views (unless RETAIL_DATA=off) plus onboarded dataset views (datasets/*.json)."""
    from . import datasets

    views = dict(_catalog()["views"]) if retail_enabled() else {}
    for name, view in datasets.catalog_entries().items():
        views.setdefault(name, view)
    return views


def pii_columns() -> set[str]:
    cols: set[str] = set()
    for view in all_views().values():
        for name, meta in view["columns"].items():
            if meta.get("pii"):
                cols.add(name)
    return cols


_RESTRICTIVENESS = {"full": 0, "partial": 1, "pseudonym": 2, "redacted": 3}


def pii_visibility(tier: str) -> dict[str, str]:
    """Column name → what this tier sees. Dataset columns follow their mask kind;
    a name used with different kinds gets the most restrictive answer."""
    from .datasets import MASK_KINDS

    vis = dict(_catalog()["pii_visibility"][tier])
    for view in all_views().values():
        for name, meta in view["columns"].items():
            kind = meta.get("mask")
            if not kind or kind not in MASK_KINDS:
                continue
            v = MASK_KINDS[kind][tier]
            if name not in vis or _RESTRICTIVENESS[v] > _RESTRICTIVENESS.get(vis[name], 0):
                vis[name] = v
    return vis


def visible_columns(tier: str) -> set[str]:
    """PII columns whose real value (fully or partly) this tier can see."""
    return {c for c, v in pii_visibility(tier).items() if v in {"full", "partial"}}


def filterable_pii(tier: str) -> set[str]:
    """PII columns this tier may filter / sort / join on.

    Full tier: all. Partial tier: only columns it sees in full (city).
    Masked tier: none — a filter would run against the masked value and
    silently return the wrong rows.
    """
    return {c for c, v in pii_visibility(tier).items() if v == "full"}


def views_for(user: User) -> dict:
    return {name: v for name, v in all_views().items() if user.role in v["roles"]}


def catalog_for(user: User) -> dict:
    vis = pii_visibility(user.pii_tier)
    out = {}
    for name, view in views_for(user).items():
        cols = {}
        for col, meta in view["columns"].items():
            entry = {"type": meta["type"], "description": meta["description"]}
            if meta.get("pii"):
                entry["pii"] = True
                entry["you_see"] = vis.get(col, "redacted")
            cols[col] = entry
        out[name] = {"description": view["description"], "columns": cols}
    return out


def glossary() -> dict:
    return _glossary()["terms"] if retail_enabled() else {}


def unsupported() -> dict[str, list[str]]:
    if not retail_enabled():
        return {}
    return {k: v for k, v in _glossary()["unsupported"].items() if not k.startswith("_")}


def render_catalog(user: User) -> str:
    """Compact text form of the role-trimmed catalog for a prompt."""
    lines = []
    for name, view in catalog_for(user).items():
        lines.append(f"view {name}: {view['description']}")
        for col, meta in view["columns"].items():
            tag = f" [PII: you see {meta['you_see']}]" if meta.get("pii") else ""
            lines.append(f"  - {col} {meta['type']}{tag}: {meta['description']}")
    return "\n".join(lines)


def render_glossary(extra: list[dict] | None = None) -> str:
    from . import datasets

    lines = [f"- (dataset notes) {n}" for n in datasets.notes()]
    for term, meta in glossary().items():
        if meta.get("ambiguous"):
            opts = "; ".join(f"{c['key']}: {c['label']}" for c in meta["candidates"])
            lines.append(f"- {term} (AMBIGUOUS — ask which: {opts})")
        else:
            lines.append(f"- {term}: {meta['definition']} SQL: {meta['sql_hint']}")
    for p in extra or []:
        lines.append(f"- {p['term']}: {p['definition']} SQL: {p['sql_hint']} (approved proposal)")
    return "\n".join(lines)
