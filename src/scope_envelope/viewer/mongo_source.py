"""Real data source: a MongoDB Change Stream on `runs`, filtered to one
ticket+condition. This is the only place in the viewer that talks to Mongo.

Requires a replica set (Atlas satisfies this automatically).
"""

import queue
import threading

from ..store import connect


class MongoSource:
    def __init__(self, uri: str, db_name: str, runs_collection: str, ticket: str, condition: str):
        self._client = connect(uri)
        self._runs = self._client[db_name][runs_collection]
        self._ticket = ticket
        self._condition = condition
        self._queue: "queue.Queue[dict]" = queue.Queue()

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

    def _watch(self) -> None:
        with self._runs.watch(self._match_pipeline()) as stream:
            for change in stream:
                self._queue.put(change["fullDocument"])

    def start(self) -> None:
        threading.Thread(target=self._watch, daemon=True).start()

    def events(self):
        """Yields `runs` docs forever, in insert order."""
        while True:
            yield self._queue.get()

    def close(self) -> None:
        self._client.close()
