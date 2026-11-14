"""Live source: the Change Stream on `runs`, all runs, for as long as the server is up.

Yields the same ("run", doc) tuples as mock_source.py, so bridge.py doesn't care which one it gets.
"""

import queue
import threading

from ..store import connect, mongo_settings


class LiveSource:
    def __init__(self):
        settings = mongo_settings()
        if not settings["uri"]:
            raise SystemExit("MONGODB_URI is not set -- add it to .env")
        self._runs = connect(settings["uri"])[settings["db"]][settings["runs"]]
        self._queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()

    def _watch(self) -> None:
        # max_await_time_ms: ask Atlas for new events every 0.2s instead of the ~1s default.
        with self._runs.watch([{"$match": {"operationType": "insert"}}], max_await_time_ms=200) as stream:
            for change in stream:
                self._queue.put(("run", change["fullDocument"]))

    def start(self) -> None:
        threading.Thread(target=self._watch, daemon=True).start()

    def events(self):
        while True:
            yield self._queue.get()
