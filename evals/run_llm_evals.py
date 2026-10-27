"""Opt-in: the golden set through a REAL model backend.

    MODEL_BACKEND=ollama OLLAMA_MODEL=qwen2.5-coder:7b python evals/run_llm_evals.py
    MODEL_BACKEND=anthropic ANTHROPIC_API_KEY=... python evals/run_llm_evals.py
    MODEL_BACKEND=openai OPENAI_MODEL=... OPENAI_API_KEY=... python evals/run_llm_evals.py

Same 70 cases, same comparator, same leak and scope checks as
``run_evals.py`` — only the planner and answer writer are the model.
This is where natural-language text-to-SQL quality is measured. It is
NOT part of the default gate (it costs money and is not deterministic),
and it refuses to run on the offline planner (exit 2) rather than report
a model score it did not measure.

Security figures (leaks, scope violations) must still be zero: they do
not depend on the model, because the database enforces them.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_evals import Truth, run_golden  # noqa: E402

from sql_agent.agent import Agent  # noqa: E402
from sql_agent.config import get_settings  # noqa: E402
from sql_agent.model_backends import MockBackend, build_backend  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(Path(__file__).resolve().parent / "results.llm.json"))
    ap.add_argument("--limit", type=int, default=0, help="run only the first N cases (a cheap slice)")
    args = ap.parse_args(argv)

    settings = get_settings()
    backend = build_backend(settings)
    if isinstance(backend, MockBackend):
        print("run_llm_evals.py needs a real model: set MODEL_BACKEND=ollama (or anthropic / openai). "
              "The offline planner is evaluated by run_evals.py.", file=sys.stderr)
        return 2
    agent = Agent(settings, backend=backend)
    truth = Truth(agent)
    path = Path(__file__).resolve().parent / "golden.jsonl"
    if args.limit:
        tmp = Path(args.json).with_suffix(".slice.jsonl")
        tmp.write_text("\n".join(path.read_text().splitlines()[: args.limit]) + "\n")
        path = tmp
    golden = run_golden(agent, truth, path)

    print(f"Golden set through {backend.name}: {golden['cases']} cases")
    print(f"  behaviour accuracy   {golden['behaviour_accuracy']:.1%}")
    print(f"  execution accuracy   {golden['execution_accuracy']:.1%} ({golden['execution_cases']} answers)")
    print(f"  scope-note accuracy  {golden['scope_note_accuracy']:.1%}")
    print(f"  median latency       {golden['median_latency_ms']} ms")
    print(f"  PII leaks            {len(golden['pii_leaks'])}   (must be 0 for any model)")
    print(f"  scope violations     {len(golden['scope_violations'])}   (must be 0 for any model)")
    for cat, score in golden["by_category"].items():
        print(f"    {cat:24} {score}")
    for row in golden["results"]:
        if not row["pass"]:
            print(f"  miss {row['case_id']} [{row['user']}] {row['question']!r}: expected {row['expected']}/{row['expected_reason']}, "
                  f"got {row['got']}/{row['got_reason']}, exec={row['execution_match']}, attempts={row['attempts']}")
    Path(args.json).write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                           "backend": backend.name, "golden": golden}, indent=2, default=str))
    return 0 if not golden["pii_leaks"] and not golden["scope_violations"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
