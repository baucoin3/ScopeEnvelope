"""In-process source: events handed over directly by the agent running in the same process.

scope-agent starts the web server inside its own process, so there is no need to round-trip
every event through Atlas to reach the browser. publish() is called from the gateway's
on_event hook; the browser sees the event immediately. Atlas still gets every event.
"""

import queue

_events: "queue.Queue[tuple[str, dict]]" = queue.Queue()


def publish(doc: dict) -> None:
    _events.put(("run", dict(doc)))


class LocalSource:
    def start(self) -> None:
        pass

    def events(self):
        while True:
            yield _events.get()
