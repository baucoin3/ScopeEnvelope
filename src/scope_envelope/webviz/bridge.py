"""Bridges one or more sources' blocking events() generators to the
FastAPI event loop's asyncio.Queue. Drains each source in its own
background thread (same pattern MongoSource already uses internally for
its Change Stream watchers) and crosses the thread boundary with
loop.call_soon_threadsafe -- queue.put_nowait is not safe to call
directly from another OS thread against an asyncio.Queue; that races
with the loop and can drop/corrupt events under real timing even though
it often looks fine in a quick manual test.

Takes a *list* of sources, not one: MockSource self-plays both
conditions out of a single source, but MongoSource (viewer/mongo_source.py)
is scoped to one ticket+condition's Change Stream, so the real wiring
needs two instances (lazy, envelope) merged into the same queue. Each
source's docs carry their own `condition`, so one shared
_normalizer_for() dict still routes correctly regardless of how many
sources feed it.

Single queue, single consumer, no fan-out, no replay buffer -- a page
refresh or a second tab will not receive past events. That's an accepted
limitation for a hackathon demo (visualization-plan.md §3), not a bug.
"""

import asyncio
import threading

from .events import ConditionNormalizer, WireEvent


class Bridge:
    def __init__(self, sources: list, ticket: str, loop: asyncio.AbstractEventLoop):
        self._sources = sources
        self._ticket = ticket
        self._loop = loop
        self._normalizers: dict[str, ConditionNormalizer] = {}
        self._run_ids: dict[str, str | None] = {}
        self.queue: "asyncio.Queue[WireEvent]" = asyncio.Queue()

    def _normalizer_for(self, condition: str, run_id: str | None = None) -> ConditionNormalizer:
        # A new run of the same condition starts from a clean slate (the browser resets that panel).
        if condition in self._normalizers and run_id and self._run_ids.get(condition) != run_id:
            del self._normalizers[condition]
        self._run_ids[condition] = run_id or self._run_ids.get(condition)
        if condition not in self._normalizers:
            self._normalizers[condition] = ConditionNormalizer(self._ticket, condition)
        return self._normalizers[condition]

    def _drain(self, source) -> None:
        source.start()
        for event_type, doc in source.events():
            if not doc.get("condition"):
                continue
            normalizer = self._normalizer_for(doc["condition"], doc.get("run_id"))
            wire_event = normalizer.normalize(event_type, doc)
            self._loop.call_soon_threadsafe(self.queue.put_nowait, wire_event)

    def start(self) -> None:
        for source in self._sources:
            threading.Thread(target=self._drain, args=(source,), daemon=True).start()
