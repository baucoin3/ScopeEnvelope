"""Check the Atlas connection without printing the connection string."""

import os
import re
import sys

from dotenv import load_dotenv
from pymongo import MongoClient


def redact(message: str) -> str:
    return re.sub(r"mongodb(\+srv)?://[^\s'\"]+", "mongodb://[redacted]", message)


def main() -> int:
    load_dotenv()
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("MONGODB_URI=unset")
        return 1
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        info = client.server_info()
        print(f"ping=ok version={info.get('version')}")
        db = client[os.environ.get("MONGODB_DB", "scopeenvelope")]
        print(f"db={db.name} collections={db.list_collection_names()}")
    except Exception as exc:  # report the failure type without leaking the URI
        print(f"error={type(exc).__name__}: {redact(str(exc))[:300]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
