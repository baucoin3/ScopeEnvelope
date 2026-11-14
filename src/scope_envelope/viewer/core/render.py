"""Builds a single rich renderable from a RunState. Rebuilt from scratch on
every event -- event rate is one per tool call (seconds apart), so a full
rebuild is cheap and far simpler than hand-patching widgets (textual-style
granular reactivity is overkill at this rate).

Rich table cells can't draw a dashed border per-cell, so "in learned scope"
is approximated with a `::` marker around the filename instead of a dashed
box -- close enough for a terminal demo.
"""

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import state as state_mod

_UNTOUCHED = "untouched"
_STYLE = {
    state_mod.READ: "bold white on blue3",
    state_mod.WRITTEN: "bold white on green4",
    state_mod.BLOCKED: "bold white on red3",
    _UNTOUCHED: "grey62 on grey19",
}

_COLUMNS = 4


def _tile_text(path: str, status: str, in_scope: bool) -> Text:
    label = f"::{path}::" if in_scope else path
    return Text(f" {label} ", style=_STYLE[status], justify="center")


def _build_tile_grid(state: state_mod.RunState):
    # Tiles exist for files in the learned scope and files a tool call touched --
    # a real repo can have thousands of files, so we never draw the whole tree.
    if not state.tiles and not state.scope:
        return Text("(no files touched yet)", style="grey50")

    table = Table.grid(padding=(0, 1))
    for _ in range(_COLUMNS):
        table.add_column()

    tiles = {**{p: _UNTOUCHED for p in sorted(state.scope)}, **state.tiles}
    paths = list(tiles.keys())
    for i in range(0, len(paths), _COLUMNS):
        row = paths[i : i + _COLUMNS]
        cells = [_tile_text(p, tiles[p], p in state.scope) for p in row]
        cells += [Text("")] * (_COLUMNS - len(cells))
        table.add_row(*cells)
    return table


def _build_counters(state: state_mod.RunState) -> Table:
    table = Table.grid(padding=(0, 3))
    for _ in range(4):
        table.add_column()

    prompt_tokens = f"{state.prompt_tokens:,}" if state.prompt_tokens is not None else "--"
    cost = f"${state.cost_usd:.4f}" if state.cost_usd is not None else "--"

    table.add_row("tool calls", "blocked", "files read", "prompt tokens", "cost")
    table.add_row(
        Text(str(state.tool_calls), style="bold"),
        Text(str(state.blocked), style="bold red3" if state.blocked else "bold"),
        Text(str(state.files_read), style="bold"),
        Text(prompt_tokens, style="bold"),
        Text(cost, style="bold"),
    )
    return table


def _build_log(state: state_mod.RunState, max_lines: int = 12) -> Panel:
    lines = list(state.log)[-max_lines:]
    body = "\n".join(lines) if lines else "(waiting for events...)"
    # Log lines contain literal "[tool]" brackets -- Text() (unlike a plain
    # str passed to Panel) does not interpret them as markup.
    return Panel(Text(body), title="event log", border_style="grey50")


def build_renderable(state: state_mod.RunState) -> Panel:
    status_style = "bold green" if state.status == state_mod.DONE else "bold yellow"
    # Text.assemble, not markup: ticket ids and model names may contain [brackets].
    header = Text.assemble(
        (state.condition, "bold"), "  ", state.ticket, "   ", (state.status, status_style),
        ("   " + state.model, "grey50") if state.model else "",
    )
    if state.based_on:
        header.append(f"\nlearned from {', '.join(state.based_on)}", style="magenta")

    body = Group(
        header,
        Text(""),
        _build_tile_grid(state),
        Text(""),
        _build_counters(state),
        Text(""),
        _build_log(state),
    )
    return Panel(body, title="ScopeEnvelope viewer")
