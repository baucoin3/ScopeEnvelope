"""End-to-end check against Atlas: seed example footprints, then fetch a scope for a new ticket.

Example footprints are tagged `example: true` and replaced on every run, so this
never touches real history. Remove them with --clean.
"""

import sys
import time

from scope_envelope.gateway import extract_verb
from scope_envelope.normalize import extract_subject
from scope_envelope.store import Store

EXAMPLES = [
    ("PIPE-1801", "Implement Kafka consumer for SearchQueryEvent and register it behind a feature flag", 22),
    ("PIPE-1805", "Implement Kafka consumer for ContentViewEvent and register it behind a feature flag", 25),
    ("PIPE-1812", "Implement Kafka consumer for CartUpdatedEvent and register it behind a feature flag", 24),
    ("PIPE-1820", "Implement Kafka consumer for PageExitEvent and register it behind a feature flag", 38),
]
COMMON_CHANGED = [
    "pipeline/consumers/{snake}_consumer.py",
    "pipeline/registry.py",
    "config/feature_flags.yaml",
    "deploy/consumers.yaml",
    "tests/test_{snake}_consumer.py",
]
COMMON_LINES = [
    "enable_{snake}_consumer: false",
    "group_id: integration-{dashed}",
    "class {class}Consumer(BaseConsumer):",
    "registry.register({class}Consumer)",
]


def footprint(ticket: str, text: str, calls: int) -> dict:
    return {
        "example": True,
        "ticket": ticket,
        "text": text,
        "verb": extract_verb(text),
        "subject": extract_subject(text),
        "condition": "lazy",
        "success": True,
        "files_read": ["pipeline/consumers/user_activity_consumer.py", "pipeline/registry.py"],
        "files_changed": COMMON_CHANGED + (["utils/strings.py"] if ticket == "PIPE-1820" else []),
        "added_lines": COMMON_LINES + ([f"# only in {ticket}"] if ticket == "PIPE-1805" else []),
        "calls": calls,
        "prompt_tokens": calls * 1800,
        "cost_usd": calls * 0.0001,
    }


def main() -> int:
    store = Store.from_env()
    if store is None:
        print("MONGODB_URI=unset")
        return 1
    removed = store.footprints.delete_many({"example": True}).deleted_count
    if "--clean" in sys.argv:
        print(f"removed {removed} example footprints")
        return 0
    store.footprints.insert_many([footprint(*e) for e in EXAMPLES])
    print(f"seeded {len(EXAMPLES)} example footprints (replaced {removed})")

    if not store.wait_until_queryable(timeout_s=300):
        print("index not queryable after 5 minutes")
        return 1

    ticket = "Implement Kafka consumer for OrderPlacedEvent and register it behind a feature flag"
    for attempt in range(12):  # new documents are embedded asynchronously
        scope = store.get_scope(ticket, extract_verb(ticket), extract_subject(ticket))
        if scope.matched >= len(EXAMPLES):
            break
        time.sleep(5)
    print(f"after ~{attempt * 5}s: matched={scope.matched} confidence={scope.confidence} top_score={scope.top_score:.3f}")
    print(f"based_on={scope.based_on} median_calls={scope.median_calls} budget={scope.call_budget}")
    print("files_changed:", scope.concrete(scope.files_changed))
    print("lines:", scope.concrete(scope.lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
