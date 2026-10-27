"""Time periods, resolved in code — never guessed by the model.

``resolve(question, as_of, fiscal_start_month)`` returns one of:

- a :class:`Period` (half-open ``[start, end)`` dates + a label),
- an :class:`Ambiguous` (the phrase has two reasonable readings — "this
  year" is calendar or fiscal; "recently" is anything) with the options
  to offer, or
- ``None`` (no period mentioned).

The resolved period is handed to the model as a fact, so the model and
the reference answers agree on what "last quarter" means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class Period:
    start: date | None
    end: date | None  # exclusive
    label: str

    def sql_filter(self, column: str) -> str:
        parts = []
        if self.start:
            parts.append(f"{column} >= DATE '{self.start.isoformat()}'")
        if self.end:
            parts.append(f"{column} < DATE '{self.end.isoformat()}'")
        return " AND ".join(parts)

    def describe(self) -> str:
        if not self.start and not self.end:
            return f"{self.label} (no date filter)"
        last = (self.end - timedelta(days=1)).isoformat() if self.end else "…"
        return f"{self.label}: {self.start.isoformat() if self.start else '…'} to {last} inclusive"


@dataclass(frozen=True)
class Ambiguous:
    phrase: str
    question: str
    options: list[tuple[str, str, str]] = field(default_factory=list)  # (key, label, rewrite phrase)


def _month_start(y: int, m: int) -> date:
    return date(y, m, 1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _quarter(y: int, q: int) -> Period:
    start = date(y, 3 * (q - 1) + 1, 1)
    return Period(start, _add_months(start, 3), f"Q{q} {y}")


def _fiscal(y: int, fs: int) -> Period:
    start = date(y, fs, 1)
    return Period(start, _add_months(start, 12), f"FY{y}")


def resolve(question: str, as_of: date, fiscal_start_month: int = 2) -> Period | Ambiguous | None:
    q = " " + question.lower().replace("’", "'") + " "
    tomorrow = as_of + timedelta(days=1)
    cur_m = _month_start(as_of.year, as_of.month)

    # --- explicitly ambiguous phrases --------------------------------------
    fy_now = as_of.year if as_of.month >= fiscal_start_month else as_of.year - 1
    if re.search(r"\b(this|current) year\b|\bytd\b|\byear[- ]to[- ]date\b", q) and not re.search(r"\b(calendar|fiscal)\b", q):
        return Ambiguous(
            "this year",
            "Do you mean the calendar year or the fiscal year?",
            [("calendar", f"Calendar {as_of.year} to date (1 Jan – {as_of.isoformat()})", f"calendar {as_of.year}"),
             ("fiscal", f"FY{fy_now} to date (from 1 {MONTH_NAMES[fiscal_start_month - 1]} {fy_now})", f"fiscal {fy_now}")],
        )
    if re.search(r"\b(last|previous|prior) year\b", q) and not re.search(r"\b(calendar|fiscal)\b", q):
        return Ambiguous(
            "last year",
            "Do you mean the last calendar year or the last fiscal year?",
            [("calendar", f"Calendar {as_of.year - 1}", f"calendar {as_of.year - 1}"),
             ("fiscal", f"FY{fy_now - 1}", f"fiscal {fy_now - 1}")],
        )
    if re.search(r"\b(recently|lately|recent)\b", q):
        return Ambiguous(
            "recently",
            "Which period should 'recently' cover?",
            [("30d", "The last 30 days", "in the last 30 days"),
             ("last-month", "Last month", "last month"),
             ("last-quarter", "Last quarter", "last quarter")],
        )

    # --- all time ----------------------------------------------------------
    if re.search(r"\b(all[- ]time|ever|since launch|overall|lifetime|in total so far)\b", q):
        return Period(None, None, "all time")

    # --- fiscal years ------------------------------------------------------
    m = re.search(r"\b(?:fiscal(?: year)?|fy)\s*'?(20\d\d|\d\d)\b", q)
    if m:
        y = int(m.group(1))
        y = y + 2000 if y < 100 else y
        return _fiscal(y, fiscal_start_month)
    if re.search(r"\b(last|previous|prior) fiscal year\b", q):
        return _fiscal(fy_now - 1, fiscal_start_month)
    if re.search(r"\b(this|current) fiscal year\b", q):
        p = _fiscal(fy_now, fiscal_start_month)
        return Period(p.start, min(p.end, tomorrow), f"FY{fy_now} to date")

    # --- calendar years ----------------------------------------------------
    if re.search(r"\b(last|previous|prior) calendar year\b", q):
        return Period(date(as_of.year - 1, 1, 1), date(as_of.year, 1, 1), f"calendar {as_of.year - 1}")
    if re.search(r"\b(this|current) calendar year\b", q):
        return Period(date(as_of.year, 1, 1), tomorrow, f"calendar {as_of.year} to date")

    # --- quarters ----------------------------------------------------------
    m = re.search(r"\bq([1-4])\s*(?:of\s*)?'?(20\d\d)\b|\b(20\d\d)\s*q([1-4])\b", q)
    if m:
        qn, y = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        return _quarter(y, qn)
    cur_q = (as_of.month - 1) // 3 + 1
    if re.search(r"\b(last|previous|prior) quarter\b", q):
        y, qn = (as_of.year, cur_q - 1) if cur_q > 1 else (as_of.year - 1, 4)
        return _quarter(y, qn)
    if re.search(r"\b(this|current) quarter\b|\bquarter to date\b|\bqtd\b", q):
        p = _quarter(as_of.year, cur_q)
        return Period(p.start, min(p.end, tomorrow), f"Q{cur_q} {as_of.year} to date")
    m = re.search(r"\bq([1-4])\b", q)
    if m:
        qn = int(m.group(1))
        y = as_of.year if qn <= cur_q else as_of.year - 1
        return _quarter(y, qn)

    # --- months ------------------------------------------------------------
    if re.search(r"\b(last|previous|prior) month\b", q):
        s = _add_months(cur_m, -1)
        return Period(s, cur_m, f"{MONTH_NAMES[s.month - 1]} {s.year}")
    if re.search(r"\b(this|current) month\b|\bmonth to date\b|\bmtd\b", q):
        return Period(cur_m, tomorrow, f"{MONTH_NAMES[cur_m.month - 1]} {cur_m.year} to date")
    m = re.search(rf"\b(?:last|past|previous|trailing)\s+(\d{{1,2}})\s+months\b", q)
    if m:
        n = int(m.group(1))
        s = _add_months(cur_m, -(n - 1))
        return Period(s, tomorrow, f"the last {n} months ({MONTH_NAMES[s.month - 1]} {s.year} – {MONTH_NAMES[as_of.month - 1]} {as_of.year})")
    m = re.search(rf"\bsince\s+({_MONTH_RE})\s+(20\d\d)\b", q)
    if m:
        s = date(int(m.group(2)), MONTHS[m.group(1)], 1)
        return Period(s, tomorrow, f"since {MONTH_NAMES[s.month - 1]} {s.year}")
    m = re.search(rf"\b({_MONTH_RE})\s+(20\d\d)\b", q)
    if m:
        s = date(int(m.group(2)), MONTHS[m.group(1)], 1)
        return Period(s, _add_months(s, 1), f"{MONTH_NAMES[s.month - 1]} {s.year}")
    m = re.search(rf"\bin\s+({_MONTH_RE})\b", q)
    if m:
        mo = MONTHS[m.group(1)]
        y = as_of.year if mo <= as_of.month else as_of.year - 1
        s = date(y, mo, 1)
        return Period(s, _add_months(s, 1), f"{MONTH_NAMES[mo - 1]} {y}")

    # --- days / weeks --------------------------------------------------------
    m = re.search(r"\b(?:last|past|previous|trailing)\s+(\d{1,3})\s+days\b", q)
    if m:
        n = int(m.group(1))
        return Period(tomorrow - timedelta(days=n), tomorrow, f"the last {n} days")
    if re.search(r"\b(last|previous|prior) week\b", q):
        this_monday = as_of - timedelta(days=as_of.weekday())
        s = this_monday - timedelta(days=7)
        return Period(s, this_monday, f"the week of {s.isoformat()}")
    if re.search(r"\b(this|current) week\b", q):
        s = as_of - timedelta(days=as_of.weekday())
        return Period(s, tomorrow, f"the week of {s.isoformat()} to date")
    if re.search(r"\byesterday\b", q):
        return Period(as_of - timedelta(days=1), as_of, (as_of - timedelta(days=1)).isoformat())
    if re.search(r"\btoday\b", q):
        return Period(as_of, tomorrow, as_of.isoformat())

    # --- bare years = calendar years (the label says so) ---------------------
    m = re.search(r"\b(?:in|during|for|calendar|of)\s+(20\d\d)\b|\b(20\d\d)\b", q)
    if m:
        y = int(m.group(1) or m.group(2))
        end = date(y + 1, 1, 1)
        label = f"calendar {y}" + (" to date" if end > tomorrow else "")
        return Period(date(y, 1, 1), min(end, tomorrow), label)

    return None
