"""Scripted run for building and demoing the viewer without an LLM.

Drives a real Gateway through a fixed sequence of tool calls on a copy of the
toy fixture repo, so the viewer sees exactly the events a real run produces.
Writes to the `runs` collection in Atlas.

    scope-envelope mock "toy-1: Make greet shout" --condition envelope
"""

import shutil
import tempfile
import time
from pathlib import Path

from ..gateway import Gateway
from ..store import Store

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "toy_repo"

STEPS = [
    ("list_dir", {"path": "."}),
    ("read_file", {"path": "README.md"}),
    ("read_file", {"path": "app.py"}),
    ("grep", {"pattern": "shout", "path": "."}),
    ("read_file", {"path": "utils.py"}),
    ("write_file", {"path": "test_app.py", "content": "# rewritten\n"}),
    ("write_file", {"path": "app.py", "content": "from utils import shout\n\n\ndef greet(name):\n    return shout(f'Hello, {name}')\n"}),
    ("run_tests", {}),
]


def run(ticket: str, condition: str, delay: float = 1.5) -> dict:
    store = Store.from_env()
    if store is None:
        raise SystemExit("MONGODB_URI is not set -- add it to .env")
    if not FIXTURE.is_dir():
        raise SystemExit(f"fixture repo not found at {FIXTURE} (mock needs a source checkout)")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "toy_repo"
        shutil.copytree(FIXTURE, repo)
        gw = Gateway(ticket, ticket, repo, condition, store=store, verbose=True)
        for tool, args in STEPS:
            gw.record_llm_step(prompt_tokens=1500 + 400 * gw.step, completion_tokens=120, cost_usd=0.0015, cost_source="price_table")
            gw.call(tool, args)
            time.sleep(delay)
        # No checks: a scripted run must never be saved as learning history.
        return gw.finish({"passed": 0, "total": 0})
