"""create_agent loop + run_ticket(ticket, condition) -> dict.

Every tool call goes through call_tool() (stub_gateway today). Swapping in
a real gateway later is a one-line change: replace the `import
stub_gateway as gateway` line below.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_openai import ChatOpenAI

import stub_gateway as gateway

load_dotenv()

REPO_ROOT = str(Path(__file__).parent / "toy_repo")
RUNS_LOG = Path(__file__).parent / "runs.jsonl"
RECURSION_LIMIT = 15  # hard step cap so a run can't loop forever

BASE_SYSTEM_PROMPT = (
    "You are a coding agent working inside a single repository. "
    "You are given a Jira-style ticket describing a change to make. "
    "Use the available tools to explore the repo, read relevant files, "
    "make the smallest edit that satisfies the ticket, and verify your "
    "change with run_tests. Stay inside the repo -- all paths are "
    "relative to the repo root. When you are done, summarize what you "
    "changed in your final answer."
)

# Per-run tracking state, reset at the start of each run_ticket() call.
_run_state = {
    "ticket": None,
    "condition": None,
    "calls": 0,
    "files_read": set(),
    "files_changed": set(),
}


def _call_tool(name: str, args: dict) -> dict:
    # calls is incremented before dispatch, so it doubles as the step number.
    _run_state["calls"] += 1
    return gateway.call_tool(
        name,
        args,
        REPO_ROOT,
        ticket=_run_state["ticket"],
        condition=_run_state["condition"],
        step=_run_state["calls"],
    )


@tool
def list_dir(path: str = ".") -> dict:
    """List files and directories at a path relative to the repo root."""
    return _call_tool("list_dir", {"path": path})


@tool
def read_file(path: str) -> dict:
    """Read the full contents of a file relative to the repo root."""
    _run_state["files_read"].add(path)
    return _call_tool("read_file", {"path": path})


@tool
def grep(pattern: str, path: str = ".") -> dict:
    """Search for a regex pattern in files under a path relative to the repo root."""
    return _call_tool("grep", {"pattern": pattern, "path": path})


@tool
def write_file(path: str, content: str) -> dict:
    """Overwrite (or create) a file relative to the repo root with the given content."""
    _run_state["files_changed"].add(path)
    return _call_tool("write_file", {"path": path, "content": content})


@tool
def run_tests() -> dict:
    """Run the repo's test suite and return pass/fail output."""
    return _call_tool("run_tests", {})


TOOLS = [list_dir, read_file, grep, write_file, run_tests]


def _build_model() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.environ["MODEL"],
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
    )


def run_ticket(ticket: str, condition: str) -> dict:
    _run_state["ticket"] = ticket
    _run_state["condition"] = condition
    _run_state["calls"] = 0
    _run_state["files_read"] = set()
    _run_state["files_changed"] = set()

    system_prompt = BASE_SYSTEM_PROMPT
    if condition == "envelope":
        scope = gateway.get_scope(ticket)
        # TODO: once get_scope returns real data, decide whether scope is
        # injected into the system prompt (as here) or the first user
        # message instead, and test both.
        system_prompt = f"{BASE_SYSTEM_PROMPT}\n\nScope: {scope}"

    agent = create_agent(model=_build_model(), tools=TOOLS, system_prompt=system_prompt)

    result = agent.invoke(
        {"messages": [{"role": "user", "content": ticket}]},
        config={"recursion_limit": RECURSION_LIMIT},
    )

    messages = result["messages"]
    prompt_tokens = 0
    completion_tokens = 0
    for msg in messages:
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            prompt_tokens += usage.get("input_tokens", 0)
            completion_tokens += usage.get("output_tokens", 0)

    final_answer = messages[-1].content if messages else ""

    record = {
        "ticket": ticket,
        "condition": condition,
        "calls": _run_state["calls"],
        "files_read": sorted(_run_state["files_read"]),
        "files_changed": sorted(_run_state["files_changed"]),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": None,  # TODO: pricing for `MODEL` is not wired up yet
        "final_answer": final_answer,
    }

    with open(RUNS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

    gateway.save_footprint(ticket, condition, record["calls"], prompt_tokens, record["cost_usd"])

    return record
