"""Pure in-memory state model for one live run. No I/O here -- sources feed
it normalized event dicts, render.py reads it. Keeping this free of Mongo/
rich imports is what makes it testable against the mock generator or a real
gateway without any code changes here.
"""

from collections import deque
from dataclasses import dataclass, field

READ = "read"
WRITTEN = "written"
BLOCKED = "blocked"

RUNNING = "running"
DONE = "done"

_LOG_CAPACITY = 200


@dataclass
class RunState:
    ticket: str
    condition: str
    tiles: dict[str, str]
    scope: set[str] = field(default_factory=set)
    status: str = RUNNING
    tool_calls: int = 0
    files_read: set[str] = field(default_factory=set)
    prompt_tokens: int | None = None
    cost_usd: float | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=_LOG_CAPACITY))


def new_state(ticket: str, condition: str) -> RunState:
    # tiles start empty -- a real repo can have hundreds of files, so we only
    # ever draw the ones a tool call actually touches, not the whole tree.
    return RunState(ticket=ticket, condition=condition, tiles={})


def apply_run_event(state: RunState, doc: dict) -> None:
    """One `runs` doc == one tool call. Mutates state in place."""
    tool = doc.get("tool", "?")
    path = doc.get("path")
    decision = doc.get("decision", "allowed")
    reason = doc.get("reason")
    scope_based_on = doc.get("scope_based_on")

    if scope_based_on and not state.scope:
        state.scope = set(scope_based_on)

    state.tool_calls += 1

    if path:
        if tool == "write_file":
            state.tiles[path] = BLOCKED if decision == "blocked" else WRITTEN
        elif tool == "read_file" and decision != "blocked":
            state.tiles.setdefault(path, READ)
            state.files_read.add(path)

    line = f"[{tool}]"
    if path:
        line += f" {path}"
    if decision == "blocked":
        line += " x blocked"
        if reason:
            line += f" ({reason})"
    state.log.append(line)


def apply_footprint_event(state: RunState, doc: dict) -> None:
    """A `footprints` doc for this ticket means the run is actually done --
    this is the only place prompt_tokens/cost_usd come from.
    """
    state.prompt_tokens = doc.get("prompt_tokens")
    state.cost_usd = doc.get("cost_usd")
    state.status = DONE
    calls = doc.get("calls", state.tool_calls)
    state.log.append(f"done -- {calls} calls, {state.prompt_tokens} prompt tokens, ${state.cost_usd}")
