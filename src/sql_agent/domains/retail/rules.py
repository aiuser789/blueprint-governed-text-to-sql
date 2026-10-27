"""Question-screen rules that only make sense for the retail data.

``screens.py`` applies these when the retail data is in play (see
``knowledge.retail_enabled()``); they never fire for an onboarded dataset.
"""

from __future__ import annotations

import re

from .seed import REGIONS

_EXTRA_CITIES = [
    "Charlotte", "Raleigh", "Orlando", "Tampa", "Austin", "San Antonio", "Los Angeles", "San Francisco", "San Jose",
    "Sacramento", "Las Vegas", "Salt Lake City", "Kansas City", "St. Louis", "Indianapolis", "Cleveland", "Cincinnati",
    "Louisville", "Memphis", "New Orleans", "Oklahoma City", "Albuquerque", "Tucson", "Omaha", "Richmond", "Washington",
    "Newark", "Hartford", "Providence", "Buffalo", "Fort Mill",
]

CITIES = sorted({c for cities in REGIONS.values() for c, _ in cities} | set(_EXTRA_CITIES), key=len, reverse=True)

METRIC_NEEDS_PERIOD = re.compile(
    r"\b(revenue|sales|turnover|gmv|aov|average order|orders?|units|items sold|new customers|signups|top \d*\s*(customers|products|reps)|best[- ]selling|best customers|biggest customers)\b"
)
STATE_QUESTION = re.compile(
    r"\b(how many customers|number of customers|customer count|customers do (i|we) have|list (my |all |the )?customers|customers by (segment|region|age)|age band|age group|which reps|list (my |the )?reps|segments?\b(?!.*revenue))"
)


def needs_period(question: str) -> bool:
    """A retail metric (revenue, orders, units, top customers …) asked without a period."""
    q = question.lower()
    return bool(METRIC_NEEDS_PERIOD.search(q)) and not STATE_QUESTION.search(q) and "active customer" not in q


def unsupported_message(concept: str) -> str:
    return (f"The data I can query has no {concept} information (no cost, returns, inventory, payroll, "
            "marketing or web data). It covers customers, orders, order lines, products, sales reps and "
            "monthly summaries — I can answer revenue, order, unit and customer questions instead.")
