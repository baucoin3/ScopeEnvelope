"""Pure in-memory state model for one live run. No I/O here -- sources feed
it gateway `runs` events, render.py reads it. Keeping this free of Mongo/
rich imports is what makes it testable without either.
"""

from collections import deque
from dataclasses import dataclass, field

READ = "read"
WRITTEN = "written"
BLOCKED = "blocked"

RUNNING = "running"
DONE = "done"

_LOG_CAPACITY = 200
# Events that describe the run itself rather than a tool call.
_RUN_EVENTS = {"learn", "scope", "llm", "checks", "finish"}


@dataclass
class RunState:
    ticket: str
    condition: str
    tiles: dict[str, str]
    scope: set[str] = field(default_factory=set)  # files in the learned scope (dashed tiles)
    based_on: list[str] = field(default_factory=list)  # past tickets the scope was learned from
    model: str | None = None
    status: str = RUNNING
    tool_calls: int = 0
    blocked: int = 0
    files_read: int = 0
    prompt_tokens: int | None = None
    cost_usd: float | None = None
    token_steps: list[int] = field(default_factory=list)  # prompt tokens per model reply (tokens/step bars)
    summary: dict | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=_LOG_CAPACITY))


def new_state(ticket: str, condition: str) -> RunState:
    # tiles start empty -- a real repo can have thousands of files, so we only
    # ever draw scope files and the ones a tool call actually touches.
    return RunState(ticket=ticket, condition=condition, tiles={})


def apply_run_event(state: RunState, doc: dict) -> None:
    """Apply one gateway `runs` event. Mutates state in place."""
    tool = doc.get("tool", "?")
    path = doc.get("path")
    decision = doc.get("decision")
    totals = doc.get("totals") or {}

    state.model = doc.get("model") or state.model
    if doc.get("scope_files") and not state.scope:
        state.scope = set(doc["scope_files"])
    if doc.get("scope_based_on") and not state.based_on:
        state.based_on = list(doc["scope_based_on"])

    # Totals are authoritative: blocked calls and run events don't count as tool calls.
    state.tool_calls = totals.get("calls", state.tool_calls)
    state.blocked = totals.get("blocked", state.blocked)
    state.files_read = totals.get("files_read", state.files_read)
    if "prompt_tokens" in totals:
        state.prompt_tokens = totals["prompt_tokens"]
    if "cost_usd" in totals:
        state.cost_usd = totals["cost_usd"]

    if tool in _RUN_EVENTS:
        _apply_run_level(state, tool, doc)
        return

    if path:
        if tool == "write_file":
            state.tiles[path] = BLOCKED if decision == "blocked" else WRITTEN
        elif tool == "read_file" and decision != "error":
            state.tiles.setdefault(path, READ)

    line = f"[{tool}]"
    if path:
        line += f" {path}"
    if decision in ("blocked", "denied", "approved", "error"):
        line += f" {decision}"
        if doc.get("reason"):
            line += f" ({doc['reason']})"
    state.log.append(line)


def _apply_run_level(state: RunState, tool: str, doc: dict) -> None:
    if tool == "learn":
        decision = doc.get("decision")
        if decision == "start":
            state.log.append(f"no recent history for {doc.get('repo')}: learning it first")
        elif decision == "progress":
            state.log.append(f"learning: {doc.get('done')}/{doc.get('total')} tickets")
        elif decision == "done":
            state.log.append(f"learned {doc.get('saved')} tickets into Atlas")
        elif decision == "fresh":
            state.log.append(f"history is recent ({doc.get('footprints')} tickets in Atlas)")
    elif tool == "scope":
        scope = doc.get("scope") or {}
        if scope:
            state.log.append(f"scope: {scope.get('confidence')} match, learned from {', '.join(scope.get('based_on') or [])}")
        else:
            state.log.append("no scope injected" if state.condition == "lazy" else "no learned scope yet")
    elif tool == "llm":
        state.token_steps.append(doc.get("step_prompt_tokens") or 0)
    elif tool == "checks":
        state.log.append(f"checks {doc.get('decision')}" + (f": {doc['reason']}" if doc.get("reason") else ""))
    elif tool == "finish":
        state.summary = doc.get("summary") or {}
        state.status = DONE
        s = state.summary
        state.log.append(
            f"done -- {s.get('calls')} calls, {s.get('prompt_tokens')} prompt tokens, "
            f"${s.get('cost_usd')}, checks {s.get('checks', {}).get('passed')}/{s.get('checks', {}).get('total')}"
        )
