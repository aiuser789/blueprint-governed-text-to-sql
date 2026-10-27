"""Question screens — deterministic checks that run BEFORE any model call.

Each screen can stop the question with a decline or a clarifying
question. They are the agent's honesty rules, kept in code so they hold
whichever model backend is configured:

- ``identity``      the question tries to change who is asking, switch
                    off a control, or get the agent to write ("pretend
                    I'm an analyst", "ignore the region filter", "set
                    app.role", "delete …"). Declined.
- ``pii``           the question asks for a PII field the asker's tier
                    would only receive as a redaction, or filters on a
                    quasi-identifier the tier cannot see. Declined with
                    the masked alternative offered.
- ``unsupported``   the concept is not in the schema (profit, returns,
                    inventory …). Declined, saying what the data has.

The retail data's own rules (its city list, which retail metrics need a
period, what the retail data cannot answer) live in ``domains/retail/rules.py``.
- ``ambiguous``     a glossary term has two approved definitions, or the
                    period is ambiguous / missing for a metric that needs
                    one. Clarified, with options.

None of these screens is the security boundary — the database is. They
exist so the agent tells the user the truth instead of returning an
empty or masked table that looks like an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import knowledge
from .periods import Ambiguous, Period
from .schemas import ClarifyOption, User
from .domains.retail import rules as retail

_IDENTITY_PATTERNS = [
    (r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|your|the|these|any)\b.{0,25}\b(rules?|instructions?|polic(?:y|ies)|restrictions?|filters?|limits?|guardrails?|masking)\b",
     "The question asks me to ignore my access rules."),
    (r"\b(pretend|act|behave|imagine|assume)\b.{0,25}\b(i am|i'm|im|you are|as|like)\b.{0,25}\b(an? )?(analyst|admin|administrator|privacy officer|superuser|super user|dba|root|manager|another user|someone else|lena|sam)\b",
     "The question asks me to act as a different user or role."),
    (r"\b(as|for|impersonate)\s+(user|the user)\s+\w+", "The question asks me to answer as another user."),
    (r"\b(bypass|disable|turn off|switch off|remove|skip|drop|lift)\b.{0,30}\b(rls|row[- ]level|security|masking|mask|filters?|polic(?:y|ies)|redaction)\b",
     "The question asks me to switch off a data control."),
    (r"\bunmask(ed|ing)?\b|\bun-mask|\bwithout (the )?mask(ing)?\b|\bunredact(ed)?\b|\braw (pii|names|emails|data)\b",
     "The question asks for unmasked data."),
    (r"\bset_config\b|\bsession_token\b|\bapp\.(role|region|user|session|pii)|\bset\s+(role|session)\b|\bcurrent_setting\b",
     "The question tries to change the session context."),
    (r"\b(all|every|other) regions?\b.{0,40}\b(even though|regardless|despite|anyway)\b",
     "The question asks for data outside your scope."),
]
_WRITE_PATTERNS = [
    r"\b(delete|remove|purge|erase|wipe)\s+(all\s+|every\s+|the\s+|these\s+|those\s+)?(rows?|records?|data|entries|orders?|customers?|products?|reps?|cancell?ed\b)",
    r"\bdelete\s+from\b|\bdrop\s+(table|view|schema|database|column|role|index)\b|\btruncate\b",
    r"\binsert\s+into\b|\b(add|insert|create)\s+(a\s+)?(new\s+)?(rows?|records?|customers?|orders?|products?)\b(?!.*\b(how many|count|number of)\b)",
    r"\bupdate\s+\w+\s+set\b|\bupdate\s+(the\s+)?(orders?|customers?|records?|rows?|prices?|status)\b",
    r"\b(alter|create)\s+(table|view|role|index|schema|function|user)\b|\bgrant\b|\brevoke\b",
    r"\b(change|modify|edit|correct|fix|set)\s+(the\s+)?(status|amount|price|region|email|address|name)\s+(of|for|on|to)\b",
]

_FIELD_KIND = {"email": "email", "phone": "phone", "street_address": "address", "postal_code": "postal",
               "date_of_birth": "dob", "rep_email": "email", "account": "account", "id_number": "id_number"}
_PII_FIELDS = {
    "account": r"\baccount numbers?\b|\bcard numbers?\b|\bibans?\b|\brouting numbers?\b|\bsort codes?\b",
    "id_number": r"\bssns?\b|\bsocial security\b|\btax ids?\b|\bpassport\b|\bnational ids?\b|\bdriver'?s? licen[cs]e\b",
    "email": r"\be-?mails?\b|\bemail address(es)?\b",
    "phone": r"\bphones?\b|\bphone numbers?\b|\btelephone\b|\bmobile numbers?\b|\bcell numbers?\b",
    "street_address": r"\b(home |street |mailing |postal )?address(es)?\b|\bwhere (they|he|she|each customer) lives?\b",
    "postal_code": r"\b(postal|zip) ?codes?\b|\bpostcodes?\b",
    "date_of_birth": r"\bdates? of birth\b|\bbirth ?days?\b|\bbirth ?dates?\b|\bdobs?\b|\bhow old\b|\bages? of\b",
    "contact": r"\bcontact (details|info|information)\b",
    "rep_email": r"\brep(?:'s)? e-?mails?\b",
}
_NAME_FILTER = re.compile(r"\b(named|called|whose (last |first |sur)?name|with the (last |first |sur)?name|surname|last name|family name)\b")


@dataclass
class ScreenResult:
    kind: str  # "decline" | "clarify"
    reason_code: str
    message: str
    options: list[ClarifyOption] = field(default_factory=list)


def _tier_sees(user: User, column: str) -> str:
    from .datasets import MASK_KINDS

    kind = _FIELD_KIND.get(column)
    if kind:
        return MASK_KINDS[kind][user.pii_tier]
    return knowledge.pii_visibility(user.pii_tier).get(column, "redacted")


def screen_identity(question: str) -> ScreenResult | None:
    q = question.lower()
    for pattern, message in _IDENTITY_PATTERNS:
        if re.search(pattern, q):
            return ScreenResult(
                "decline", "identity_change",
                f"{message} I can't do that: who you are and what you may see come from your sign-in, not from the question. "
                "Ask the same question without that part and I'll answer it within your access.",
            )
    for pattern in _WRITE_PATTERNS:
        if re.search(pattern, q):
            return ScreenResult(
                "decline", "write_request",
                "I'm read-only: I can answer questions about the data, but I can't change, delete or create anything.",
            )
    return None


def screen_pii(question: str, user: User) -> ScreenResult | None:
    q = question.lower()
    if user.pii_tier == "full":
        return None
    for column, pattern in _PII_FIELDS.items():
        if not re.search(pattern, q):
            continue
        cols = ["email", "phone", "street_address"] if column == "contact" else [column]
        hidden = [c for c in cols if _tier_sees(user, c) == "redacted"]
        if hidden:
            pretty = ", ".join({"account": "account or card numbers", "id_number": "ID numbers"}.get(c, c.replace("_", " ")) for c in hidden)
            return ScreenResult(
                "decline", "pii_above_tier",
                f"Your access level ({user.pii_tier} PII) doesn't include customer {pretty}, so I can't list it. "
                "I can give you the same answer with customers identified by their masked name or customer ID instead.",
            )
    if _NAME_FILTER.search(q) and (user.pii_tier == "masked" or re.search(r"\b(surname|last name|family name)\b", q)):
        return ScreenResult(
            "decline", "pii_filter",
            f"Customer names are masked at your access level ({user.pii_tier} PII), so I can't look customers up by name — "
            "a name filter would match nothing real. I can look a customer up by customer ID, or answer by region or segment.",
        )
    if _tier_sees(user, "city") == "redacted":
        for city in retail.CITIES:
            if re.search(rf"\b{re.escape(city.lower())}\b", q):
                return ScreenResult(
                    "decline", "pii_filter",
                    f"City is masked at your access level, so I can't filter customers by {city} — the result would be "
                    "empty or wrong rather than an answer. I can break the same question down by region instead.",
                )
    return None


def _dataset_words(question: str) -> bool:
    """Does the question use a word from an onboarded dataset's table or column names?"""
    from .datasets import vocabulary

    vocab = vocabulary()
    return bool(vocab) and any(w in vocab for w in re.findall(r"[a-z]{4,}", question.lower()))


def screen_unsupported(question: str) -> ScreenResult | None:
    q = question.lower()
    from .datasets import vocabulary

    vocab = vocabulary()
    for concept, words in knowledge.unsupported().items():
        for w in words:
            # A concept missing from the retail schema may exist in an onboarded dataset.
            if any(part in vocab for part in re.findall(r"[a-z]{4,}", w)):
                continue
            if re.search(rf"\b{re.escape(w)}\b", q):
                return ScreenResult("decline", "unanswerable", retail.unsupported_message(concept))
    return None


def screen_ambiguity(question: str, period: Period | Ambiguous | None) -> ScreenResult | None:
    q = question.lower()
    for term, meta in knowledge.glossary().items():
        if not meta.get("ambiguous"):
            continue
        if not any(re.search(rf"\b{re.escape(a)}\b", q) for a in meta["aliases"]):
            continue
        if _dataset_words(question):  # e.g. "active accounts" in a banking dataset: not the retail term
            continue
        if any(m in q for c in meta["candidates"] for m in c["markers"]):
            continue
        return ScreenResult(
            "clarify", "ambiguous_term",
            f"'{term.title()}' has two approved definitions. Which one do you mean?",
            [ClarifyOption(key=c["key"], label=c["label"], rewrite=f"{question.rstrip('?')} ({c['key']} definition)")
             for c in meta["candidates"]],
        )
    if isinstance(period, Ambiguous):
        return ScreenResult(
            "clarify", "ambiguous_period", period.question,
            [ClarifyOption(key=k, label=label, rewrite=re.sub(re.escape(period.phrase), rw, question, flags=re.I)
                           if re.search(re.escape(period.phrase), question, re.I) else f"{question.rstrip('?')} ({rw})")
             for k, label, rw in period.options],
        )
    if knowledge.retail_enabled() and period is None and retail.needs_period(q) and not _dataset_words(question):
        return ScreenResult(
            "clarify", "missing_period",
            "Which period should I use?",
            [ClarifyOption(key="last-month", label="Last month", rewrite=f"{question.rstrip('?')} last month"),
             ClarifyOption(key="last-quarter", label="Last quarter", rewrite=f"{question.rstrip('?')} last quarter"),
             ClarifyOption(key="last-12-months", label="The last 12 months", rewrite=f"{question.rstrip('?')} in the last 12 months"),
             ClarifyOption(key="all-time", label="All time", rewrite=f"{question.rstrip('?')} all time")],
        )
    return None


def run_screens(question: str, user: User, period: Period | Ambiguous | None) -> tuple[ScreenResult | None, list[tuple[str, str]]]:
    """Run every screen in order; return the first stop and a trace."""
    trace: list[tuple[str, str]] = []
    for name, fn in (
        ("identity", lambda: screen_identity(question)),
        ("pii", lambda: screen_pii(question, user)),
        ("unsupported", lambda: screen_unsupported(question)),
        ("ambiguity", lambda: screen_ambiguity(question, period)),
    ):
        hit = fn()
        trace.append((name, hit.reason_code if hit else "pass"))
        if hit:
            return hit, trace
    return None, trace


def scope_line(user: User) -> str:
    tier = {"full": "full PII", "partial": "PII partially masked", "masked": "customer PII masked"}[user.pii_tier]
    if user.role == "supervisor_officer":
        return f"{user.region} region only · {tier}"
    if user.role == "sales_rep":
        return f"Your assigned customers ({user.rep_id}) only · {tier}"
    if user.role == "contractor":
        return "Monthly aggregates only (groups under 10 suppressed) · no customer rows"
    return f"All regions · {tier}"


def scope_note(question: str, user: User) -> str | None:
    """Say so when the question reaches past the asker's scope."""
    q = question.lower()
    named = [r for r in retail.REGIONS if re.search(rf"\b{r.lower()}\b", q)]
    wants_all = re.search(r"\b(all|every|each|other|by|per) regions?\b|\bacross regions\b|\bcompany[- ]wide\b|\bwhole company\b", q)
    if user.role == "supervisor_officer":
        outside = [r for r in named if r != user.region]
        if outside or wants_all:
            if outside:
                what = ", ".join(outside) + (" data is" if len(outside) == 1 else " data are")
                return f"You have access to {user.region} region data only, so {what} not included in this result."
            return f"You have access to {user.region} region data only, so other regions are not included — this shows {user.region} only."
    if user.role == "sales_rep" and (named or wants_all or re.search(r"\b(company|everyone|all customers|total)\b", q)):
        return f"You have access to your assigned customers ({user.rep_id}) only, so this covers your book, not the region or company."
    return None
