"""Mock data source: same .start()/.events()/.close() interface as
MongoSource (viewer/mongo_source.py), but replays two hardcoded step
sequences -- one for `lazy`, one for `envelope` -- on a timer instead of
watching Mongo Change Streams. Field parity with the real runs/footprints
schema (PLAN.md) is deliberate, including per-call prompt_tokens/cost_usd
that the real gateway doesn't populate yet (known gap, see
visualization-plan.md §2/§8) -- this is what makes bridge.py's
normalization code testable against the full contract today.

    python -m webviz.mock_source
"""

import queue
import threading
import time

SCOPE = ["app.py", "utils.py"]

# No scope guidance -- wanders across the whole repo, writes anywhere,
# nothing ever gets blocked. Deliberately more calls / more tokens than
# envelope to make the cost contrast visible on the shared trend chart.
LAZY_STEPS = [
    {"tool": "list_dir", "path": ".", "decision": "allowed", "reason": None, "prompt_tokens": 1800, "cost_usd": 0.00012},
    {"tool": "read_file", "path": "README.md", "decision": "allowed", "reason": None, "prompt_tokens": 2100, "cost_usd": 0.00014},
    {"tool": "read_file", "path": "app.py", "decision": "allowed", "reason": None, "prompt_tokens": 3400, "cost_usd": 0.00022},
    {"tool": "grep", "path": ".", "decision": "allowed", "reason": None, "prompt_tokens": 2600, "cost_usd": 0.00017},
    {"tool": "read_file", "path": "utils.py", "decision": "allowed", "reason": None, "prompt_tokens": 3100, "cost_usd": 0.00020},
    {"tool": "read_file", "path": "test_app.py", "decision": "allowed", "reason": None, "prompt_tokens": 2900, "cost_usd": 0.00019},
    {"tool": "write_file", "path": "app.py", "decision": "allowed", "reason": None, "prompt_tokens": 8700, "cost_usd": 0.00061},
    {"tool": "write_file", "path": "utils.py", "decision": "allowed", "reason": None, "prompt_tokens": 9200, "cost_usd": 0.00065},
    {"tool": "write_file", "path": "test_app.py", "decision": "allowed", "reason": None, "prompt_tokens": 8100, "cost_usd": 0.00057},
    {"tool": "run_tests", "path": None, "decision": "allowed", "reason": None, "prompt_tokens": 1500, "cost_usd": 0.00010},
]
LAZY_DELAYS = [0.3, 1.6, 0.5, 2.1, 0.8, 1.2, 2.6, 0.6, 1.9, 0.4]

# Scope = [app.py, utils.py] -- one write outside scope gets blocked, and
# the next successful write comes right back to a previously-touched node
# (app.py), not the blocked one. This is the blocked->reroute moment the
# animation needs to prove out.
ENVELOPE_STEPS = [
    {"tool": "list_dir", "path": ".", "decision": "allowed", "reason": None, "prompt_tokens": 1200, "cost_usd": 0.00008},
    {"tool": "read_file", "path": "app.py", "decision": "allowed", "reason": None, "prompt_tokens": 1900, "cost_usd": 0.00013},
    {"tool": "read_file", "path": "utils.py", "decision": "allowed", "reason": None, "prompt_tokens": 1700, "cost_usd": 0.00011},
    {"tool": "write_file", "path": "test_app.py", "decision": "blocked", "reason": "outside scope", "prompt_tokens": 2200, "cost_usd": 0.00015},
    {"tool": "write_file", "path": "app.py", "decision": "allowed", "reason": None, "prompt_tokens": 4300, "cost_usd": 0.00030},
    {"tool": "write_file", "path": "utils.py", "decision": "allowed", "reason": None, "prompt_tokens": 4100, "cost_usd": 0.00029},
    {"tool": "run_tests", "path": None, "decision": "allowed", "reason": None, "prompt_tokens": 1300, "cost_usd": 0.00009},
]
ENVELOPE_DELAYS = [0.4, 2.0, 0.7, 1.1, 0.5, 2.4, 0.6]


class MockSource:
    """Plays lazy's full sequence, then envelope's, in-process -- the "two
    runs, one after another" shape of the orchestrator, no agent/gateway
    required.
    """

    def __init__(self, ticket: str):
        self._ticket = ticket
        self._queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()

    def _play_condition(self, condition: str, steps: list[dict], delays: list[float]) -> None:
        scope = SCOPE if condition == "envelope" else []
        total_calls = 0
        prompt_tokens_total = 0
        cost_usd_total = 0.0

        for step_num, (step, delay) in enumerate(zip(steps, delays), start=1):
            time.sleep(delay)
            self._queue.put((
                "run",
                {
                    "ticket": self._ticket,
                    "condition": condition,
                    "step": step_num,
                    "tool": step["tool"],
                    "path": step["path"],
                    "decision": step["decision"],
                    "reason": step["reason"],
                    "prompt_tokens": step["prompt_tokens"],
                    "cost_usd": step["cost_usd"],
                    "scope_based_on": scope,
                },
            ))
            total_calls += 1
            prompt_tokens_total += step["prompt_tokens"]
            cost_usd_total += step["cost_usd"]

        time.sleep(0.3)
        self._queue.put((
            "footprint",
            {
                "ticket": self._ticket,
                "condition": condition,
                "calls": total_calls,
                "prompt_tokens": prompt_tokens_total,
                "cost_usd": round(cost_usd_total, 6),
            },
        ))

    def _play_both(self) -> None:
        self._play_condition("lazy", LAZY_STEPS, LAZY_DELAYS)
        self._play_condition("envelope", ENVELOPE_STEPS, ENVELOPE_DELAYS)

    def start(self) -> None:
        threading.Thread(target=self._play_both, daemon=True).start()

    def events(self):
        """Yields (event_type, doc) tuples -- "run" or "footprint", same
        shape as MongoSource.events(). Unlike MongoSource (which watches
        forever), this stops once both conditions have sent their
        footprint.
        """
        done_count = 0
        while done_count < 2:
            event_type, doc = self._queue.get()
            yield event_type, doc
            if event_type == "footprint":
                done_count += 1

    def close(self) -> None:
        pass


if __name__ == "__main__":
    source = MockSource(ticket="PIPE-2031: Implement Kafka consumer")
    source.start()
    for event_type, doc in source.events():
        print(f"[{doc.get('condition'):>8}] {event_type:>9}: {doc}")
