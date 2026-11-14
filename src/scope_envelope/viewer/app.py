"""Live terminal viewer for a single ScopeEnvelope run.

    scope-envelope view owner/repo#123 --condition envelope

Watches the Change Stream on `runs` for one ticket+condition and renders a live
tile grid, counters and event log. Start it before the run begins so no events
are missed.
"""

from rich.live import Live

from ..store import mongo_settings
from .core import render
from .core import state as state_mod
from .mongo_source import MongoSource


def watch(ticket: str, condition: str) -> None:
    settings = mongo_settings()
    if not settings["uri"]:
        raise SystemExit("MONGODB_URI is not set -- add it to .env")

    run_state = state_mod.new_state(ticket, condition)
    source = MongoSource(settings["uri"], settings["db"], settings["runs"], ticket, condition)
    source.start()

    with Live(render.build_renderable(run_state), refresh_per_second=4, screen=False) as live:
        for doc in source.events():
            state_mod.apply_run_event(run_state, doc)
            live.update(render.build_renderable(run_state))
            if run_state.status == state_mod.DONE:
                break

    source.close()
