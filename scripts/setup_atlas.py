"""Create the collections and the Vector Search index (Automated Embedding) in Atlas."""

import re
import sys

from scope_envelope.store import INDEX_NAME, Store


def main() -> int:
    store = Store.from_env()
    if store is None:
        print("MONGODB_URI=unset")
        return 1
    try:
        print(f"{INDEX_NAME}: {store.ensure_indexes()}")
        for ix in store.footprints.list_search_indexes():
            print(f"  {ix['name']} type={ix.get('type')} status={ix.get('status')} queryable={ix.get('queryable')}")
    except Exception as exc:
        print(f"error={type(exc).__name__}: {re.sub(r'mongodb(\+srv)?://\S+', '[redacted]', str(exc))[:400]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
