"""Real data source: MongoDB Change Streams on `runs` and `footprints`,
filtered to one ticket+condition. This is the only place that talks to
Mongo -- app.py and core/ know nothing about it, so swapping in the real
gateway's writes later (vs. mock_generate.py's fake ones) is transparent.

Requires a replica set (Atlas satisfies this automatically).
"""

import queue
import threading

from pymongo import MongoClient


class MongoSource:
    def __init__(self, uri: str, db_name: str, runs_collection: str, footprints_collection: str,
                 ticket: str, condition: str):
        self._client = MongoClient(uri)
        db = self._client[db_name]
        self._runs = db[runs_collection]
        self._footprints = db[footprints_collection]
        self._ticket = ticket
        self._condition = condition
        self._queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()

    def _match_pipeline(self) -> list[dict]:
        return [
            {
                "$match": {
                    "operationType": "insert",
                    "fullDocument.ticket": self._ticket,
                    "fullDocument.condition": self._condition,
                }
            }
        ]

    def _watch(self, collection, event_type: str) -> None:
        with collection.watch(self._match_pipeline(), full_document="updateLookup") as stream:
            for change in stream:
                self._queue.put((event_type, change["fullDocument"]))

    def start(self) -> None:
        threading.Thread(target=self._watch, args=(self._runs, "run"), daemon=True).start()
        threading.Thread(target=self._watch, args=(self._footprints, "footprint"), daemon=True).start()

    def events(self):
        """Yields (event_type, doc) tuples forever -- "run" or "footprint"."""
        while True:
            yield self._queue.get()

    def close(self) -> None:
        self._client.close()
