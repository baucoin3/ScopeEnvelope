#!/usr/bin/env python3
"""Live terminal viewer for a single ScopeEnvelope run.

    python -m viewer.app "PIPE-2031: Implement Kafka consumer" --condition envelope

Watches Change Streams on `runs` (per-call events) and `footprints` (the
completion signal + authoritative prompt_tokens/cost_usd) for one
ticket+condition, and renders a live tile grid + counters + event log.

Start this before the run (mock or real) begins, so no events are missed.
"""

import argparse

from rich.live import Live

from . import config
from .core import render
from .core import state as state_mod
from .mongo_source import MongoSource


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket", help="Ticket id/text to watch, must match the writer's `ticket` field.")
    parser.add_argument("--condition", choices=["lazy", "envelope"], default="envelope")
    args = parser.parse_args()

    if not config.MONGODB_URI:
        raise SystemExit("MONGODB_URI is not set -- add it to .env")

    run_state = state_mod.new_state(args.ticket, args.condition)

    source = MongoSource(
        config.MONGODB_URI,
        config.DB_NAME,
        config.RUNS_COLLECTION,
        config.FOOTPRINTS_COLLECTION,
        args.ticket,
        args.condition,
    )
    source.start()

    with Live(render.build_renderable(run_state), refresh_per_second=4, screen=False) as live:
        for event_type, doc in source.events():
            if event_type == "run":
                state_mod.apply_run_event(run_state, doc)
            else:
                state_mod.apply_footprint_event(run_state, doc)

            live.update(render.build_renderable(run_state))

            if run_state.status == state_mod.DONE:
                break

    source.close()


if __name__ == "__main__":
    main()
