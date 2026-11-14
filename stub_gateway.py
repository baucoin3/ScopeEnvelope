"""Stub gateway. Later, a real gateway (built elsewhere) replaces this
module: it will enforce scope, block writes outside the scope, add auth
and logging. Swapping it in should be a one-line change in agent.py
(import stub_gateway as gateway -> import real_gateway as gateway).

Also writes one `runs` doc per tool call to Mongo, in the same shape the
real gateway will use -- so the viewer can be pointed at this stub today.
"""

import os

from dotenv import load_dotenv
from pymongo import MongoClient

import tools

load_dotenv()

MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB = os.environ.get("MONGODB_DB", "scopeenvelope")
RUNS_COLLECTION = os.environ.get("RUNS_COLLECTION", "runs")
FOOTPRINTS_COLLECTION = os.environ.get("FOOTPRINTS_COLLECTION", "footprints")

_client = MongoClient(MONGODB_URI) if MONGODB_URI else None
_db = _client[MONGODB_DB] if _client is not None else None

_DISPATCH = {
    "list_dir": tools.list_dir,
    "read_file": tools.read_file,
    "grep": tools.grep,
    "write_file": tools.write_file,
    "run_tests": tools.run_tests,
}


def get_scope(ticket: str) -> list[str]:
    # TODO: real gateway computes actual scope (files/dirs the ticket is
    # allowed to touch) from the ticket text and repo analysis.
    return []


def save_footprint(ticket: str, condition: str, calls: int, prompt_tokens: int, cost_usd: float | None) -> None:
    """Called once at the end of a run. Tells the viewer the run is done."""
    if _db is None:
        return
    _db[FOOTPRINTS_COLLECTION].insert_one(
        {
            "ticket": ticket,
            "condition": condition,
            "calls": calls,
            "prompt_tokens": prompt_tokens,
            "cost_usd": cost_usd,
        }
    )


def call_tool(
    name: str,
    args: dict,
    repo_root: str,
    ticket: str | None = None,
    condition: str | None = None,
    step: int | None = None,
) -> dict:
    # TODO: real gateway enforces scope here, blocks writes outside scope,
    # adds auth/logging before dispatching to the tool.
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    result = fn(repo_root, **args)

    if _db is not None:
        _db[RUNS_COLLECTION].insert_one(
            {
                "ticket": ticket,
                "condition": condition,
                "step": step,
                "tool": name,
                "path": args.get("path"),
                "decision": "allowed",
                "reason": None,
                "scope_based_on": get_scope(ticket) if ticket else [],
            }
        )

    return result
