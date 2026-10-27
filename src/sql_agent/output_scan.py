"""Output scan — the last check before anything is shown.

The database already masks every PII column, and the model only ever
sees masked rows. This scan is defence in depth for the two places a
leak could still appear: the model's own prose (it could invent or echo
something that looks like contact data) and the result cells (should a
future view forget to mask a column).

Below full tier, a hit FAILS CLOSED: the prose is replaced by a
templated summary and any offending cells are withheld, and the audit
row records ``output_scan = 'blocked'``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
PHONE = re.compile(r"(?<![\d*])(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")
STREET = re.compile(r"\b\d{2,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Ave|Rd|Ln|Blvd|Way|Ct|Street|Avenue|Road|Lane|Drive|Dr)\b")
DOB = re.compile(r"\b(?:19[3-9]\d|200\d)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")

PATTERNS = {"email": EMAIL, "phone": PHONE, "street_address": STREET, "date_of_birth": DOB}


@dataclass
class ScanResult:
    clean: bool
    findings: list[str] = field(default_factory=list)


def scan_text(text: str) -> ScanResult:
    findings = [name for name, rx in PATTERNS.items() if rx.search(text or "")]
    return ScanResult(not findings, findings)


def scan_rows(columns: list[str], rows: list[list]) -> tuple[ScanResult, set[int]]:
    """Return findings and the indexes of columns that must be withheld."""
    bad_cols: set[int] = set()
    findings: list[str] = []
    for r in rows:
        for i, v in enumerate(r):
            if isinstance(v, str):
                for name, rx in PATTERNS.items():
                    if name == "date_of_birth" and columns[i] not in {"date_of_birth", "dob"}:
                        continue  # ordinary dates are fine in ordinary columns
                    if rx.search(v):
                        bad_cols.add(i)
                        findings.append(f"{columns[i]}:{name}")
    return ScanResult(not findings, sorted(set(findings))), bad_cols
