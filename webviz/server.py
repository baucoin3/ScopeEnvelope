"""FastAPI app: one GET /events SSE endpoint draining webviz.bridge.Bridge's
queue, plus the static frontend at /. The source (real MongoSource today;
MockSource is kept below, commented out, for visual tuning without real
data -- see the toggle comment in _startup()) is the only thing that
changes when this swaps -- no other code here changes.

    uvicorn webviz.server:app
"""

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

# from viewer import config
# from viewer.mongo_source import MongoSource
from webviz.bridge import Bridge
from webviz.mock_source import MockSource

STATIC_DIR = Path(__file__).parent / "static"
TICKET = os.environ.get("WEBVIZ_TICKET", "PIPE-2031: Implement Kafka consumer")

app = FastAPI()
_bridge: Bridge | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _bridge
    loop = asyncio.get_running_loop()

    # --- Real Mongo wiring (Milestone 5) -----------------------------------
    # MongoSource is scoped to one ticket+condition's Change Stream, so the
    # real wiring needs two instances (lazy, envelope) merged into Bridge's
    # shared queue -- unlike MockSource, which self-plays both from one.
    # To switch back to real Mongo: comment out the "Mock wiring" block
    # below, uncomment the `viewer.config`/`MongoSource` imports at the top
    # of this file and the block below.
    #
    # sources = [
    #     MongoSource(config.MONGODB_URI, config.DB_NAME, config.RUNS_COLLECTION,
    #                 config.FOOTPRINTS_COLLECTION, TICKET, "lazy"),
    #     MongoSource(config.MONGODB_URI, config.DB_NAME, config.RUNS_COLLECTION,
    #                 config.FOOTPRINTS_COLLECTION, TICKET, "envelope"),
    # ]
    # _bridge = Bridge(sources, ticket=TICKET, loop=loop)
    # _bridge.start()
    # ------------------------------------------------------------------------

    # --- Mock wiring (for tuning visuals without real data) -----------------
    source = MockSource(ticket=TICKET)
    _bridge = Bridge([source], ticket=TICKET, loop=loop)
    _bridge.start()
    # ------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    """Polled by run_both.py before it kicks off real agent runs, so the
    Mongo Change Stream watchers are guaranteed to be listening before the
    first `runs` doc is inserted."""
    return {"ready": _bridge is not None}


@app.get("/events")
async def stream_events() -> StreamingResponse:
    async def event_stream():
        while True:
            wire_event = await _bridge.queue.get()
            yield f"data: {json.dumps(wire_event.to_dict())}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
