"""FastAPI app: one GET /events SSE endpoint draining webviz.bridge.Bridge's
queue, plus the static frontend at /. The source (real MongoSource today;
MockSource is kept below, commented out, for visual tuning without real
data -- see the toggle comment in _startup()) is the only thing that
changes when this swaps -- no other code here changes.

    uvicorn scope_envelope.webviz.server:app
"""

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .bridge import Bridge
from .live_source import LiveSource
from .local_source import LocalSource
from .mock_source import MockSource

STATIC_DIR = Path(__file__).parent / "static"
TICKET = os.environ.get("WEBVIZ_TICKET", "PIPE-2031: Implement Kafka consumer")

app = FastAPI()
_bridge: Bridge | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _bridge
    loop = asyncio.get_running_loop()
    # Live Atlas Change Stream by default; WEBVIZ_MOCK=1 replays the scripted mock for tuning visuals.
    # local: events handed over in-process by scope-agent (instant); live: Atlas Change Stream; mock: scripted.
    mode = "mock" if os.environ.get("WEBVIZ_MOCK") == "1" else os.environ.get("WEBVIZ_SOURCE", "live")
    if mode == "mock":
        source = MockSource(ticket=TICKET)
    elif mode == "local":
        source = LocalSource()
    else:
        source = LiveSource()
    _bridge = Bridge([source], ticket=TICKET, loop=loop)
    _bridge.start()


def serve_in_background(host: str = "127.0.0.1", port: int = 8765) -> str:
    """Start the web viewer in a daemon thread (used by scope-agent). Returns its URL.

    Fed in-process (see local_source.publish), so the map keeps up with the agent.
    """
    import threading

    import uvicorn

    os.environ.setdefault("WEBVIZ_SOURCE", "local")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return f"http://{host}:{port}/"


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
