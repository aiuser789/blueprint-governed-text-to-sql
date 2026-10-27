"""Calling the agent over HTTP — the seam a BI tool, chat bot or TridaPad
integration would use.

    uv run uvicorn sql_agent.api:app --port 8000      # in one terminal
    uv run python examples/api_client.py              # in another

Identity travels in a header the caller's own auth layer sets (here the
demo switch, X-Demo-User). In production that header is replaced by a
verified SSO / JWT identity; the question body never carries identity.
"""

from __future__ import annotations

import json
import os
import urllib.request

BASE = os.environ.get("SQL_AGENT_URL", "http://127.0.0.1:8000")


def ask(user: str, question: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/ask",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json", "X-Demo-User": user,
                 **({"X-API-Key": os.environ["API_KEY"]} if os.environ.get("API_KEY") else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    for user in ("priya", "sam", "lena"):
        r = ask(user, "Top 5 customers by revenue last quarter")
        print(f"{user:6} {r['kind']:8} {r['scope']}")
        print(f"       {r['message']}")
        for row in r["rows"][:3]:
            print("      ", row)
