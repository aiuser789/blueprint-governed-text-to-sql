"""Which data a question is about: everything, the retail data, or one onboarded dataset.

The console's "Data" selector (and ``?data=`` in its URL) narrows the catalog
for one request. The narrowing is applied where the catalog is built, so the
prompt, the validator's table allowlist, the question screens and the
glossary all agree: a view outside the selection is not just hidden from the
model, a query against it is rejected as ``table_not_allowed``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

ALL = "all"
RETAIL = "retail"

_current: ContextVar[str] = ContextVar("data_focus", default=ALL)


def current() -> str:
    return _current.get()


def options() -> list[dict]:
    """The choices shown in the console's Data menu: each onboarded dataset (e.g. Banking), then Retail.

    Retail is always offered here — choosing it explicitly shows the retail data even when
    RETAIL_DATA=off (which only keeps the retail data out of the combined "all" view).
    """
    from . import datasets

    opts = []
    for spec in datasets.load_all()[0]:
        schema = spec["schema"]
        opts.append({"key": schema, "label": spec.get("label") or schema.replace("_", " ").title()})
    opts.append({"key": RETAIL, "label": "Retail"})
    return opts


def default() -> str:
    """The menu's starting choice: the first onboarded dataset, else Retail."""
    return options()[0]["key"]


def normalize(key: str | None) -> str:
    """Validate a requested focus; raises ValueError for one that is not on offer."""
    key = (key or ALL).strip().lower()
    if key != ALL and key not in {o["key"] for o in options()}:  # "all" stays valid for API and CLI callers
        raise ValueError(f"Unknown data selection {key!r}. Choose one of: " + ", ".join(o["key"] for o in options()))
    return key


@contextmanager
def use(key: str | None):
    token = _current.set(normalize(key))
    try:
        yield
    finally:
        _current.reset(token)
