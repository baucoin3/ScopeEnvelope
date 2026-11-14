#!/usr/bin/env python3
"""THROWAWAY scaffolding -- fakes being the gateway so the viewer can be
built and demoed before the real gateway writes real data. Inserts a
synthetic sequence of `runs` docs into Mongo on a timer, then one
`footprints` doc. Delete this once Rami's gateway is live.

    python -m viewer.mock_generate "PIPE-2031: Implement Kafka consumer" --condition envelope
"""

import argparse
import time

from pymongo import MongoClient

from . import config

SCOPE = ["app.py", "utils.py"]

STEPS = [
    {"tool": "list_dir", "path": ".", "decision": "allowed", "reason": None},
    {"tool": "read_file", "path": "README.md", "decision": "allowed", "reason": None},
    {"tool": "read_file", "path": "app.py", "decision": "allowed", "reason": None},
    {"tool": "grep", "path": ".", "decision": "allowed", "reason": None},
    {"tool": "read_file", "path": "utils.py", "decision": "allowed", "reason": None},
    {"tool": "write_file", "path": "test_app.py", "decision": "blocked", "reason": "outside scope"},
    {"tool": "write_file", "path": "app.py", "decision": "allowed", "reason": None},
    {"tool": "write_file", "path": "utils.py", "decision": "allowed", "reason": None},
    {"tool": "run_tests", "path": None, "decision": "allowed", "reason": None},
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket")
    parser.add_argument("--condition", choices=["lazy", "envelope"], default="envelope")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between fake tool calls")
    args = parser.parse_args()

    if not config.MONGODB_URI:
        raise SystemExit("MONGODB_URI is not set -- add it to .env")

    client = MongoClient(config.MONGODB_URI)
    db = client[config.DB_NAME]
    runs = db[config.RUNS_COLLECTION]
    footprints = db[config.FOOTPRINTS_COLLECTION]

    scope = SCOPE if args.condition == "envelope" else []

    for step_num, step in enumerate(STEPS, start=1):
        runs.insert_one(
            {
                "ticket": args.ticket,
                "condition": args.condition,
                "step": step_num,
                "tool": step["tool"],
                "path": step["path"],
                "decision": step["decision"],
                "reason": step["reason"],
                "scope_based_on": scope,
            }
        )
        print(f"[mock] step {step_num}: {step['tool']} {step['path']} ({step['decision']})")
        time.sleep(args.delay)

    footprints.insert_one(
        {
            "ticket": args.ticket,
            "condition": args.condition,
            "calls": len(STEPS),
            "prompt_tokens": 21340 if args.condition == "envelope" else 48910,
            "cost_usd": 0.0014 if args.condition == "envelope" else 0.0031,
        }
    )
    print("[mock] footprint written, run complete")

    client.close()


if __name__ == "__main__":
    main()
