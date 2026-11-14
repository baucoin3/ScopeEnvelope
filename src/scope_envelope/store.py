"""MongoDB Atlas: footprints (past tickets), runs (live tool calls), and the scope pipeline."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from dotenv import find_dotenv, load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.operations import SearchIndexModel

from .scope import WEAK_SCORE, Scope, scope_from_facets

INDEX_NAME = "ticket_idx"
EMBED_MODEL = os.environ.get("SCOPE_EMBED_MODEL", "voyage-4-lite")
CANDIDATES = 100
NEIGHBOURS = 6


def index_definition() -> dict:
    """Atlas Automated Embedding: Atlas embeds `text` itself with a Voyage model."""
    return {
        "fields": [
            {"type": "autoEmbed", "modality": "text", "path": "text", "model": EMBED_MODEL},
            {"type": "filter", "path": "verb"},
            {"type": "filter", "path": "success"},
        ]
    }


def _vote(field: str) -> list[dict]:
    """Keep a value only if a majority of the lean runs have it; record which tickets it came from."""
    return [
        {"$unwind": "$lean"},
        {"$unwind": f"$lean.{field}"},
        {"$group": {"_id": f"$lean.{field}", "from": {"$addToSet": "$lean.ticket"}, "n": {"$first": "$n"}}},
        {"$match": {"$expr": {"$gt": [{"$multiply": [{"$size": "$from"}, 2]}, "$n"]}}},
        {"$sort": {"_id": 1}},
    ]


def scope_pipeline(ticket_text: str, verb: str) -> list[dict]:
    """Similar successful tickets -> cheapest half -> majority vote with provenance."""
    return [
        {
            "$vectorSearch": {
                "index": INDEX_NAME,
                "path": "text",
                "query": {"text": ticket_text},
                "filter": {"verb": verb, "success": True},
                "numCandidates": CANDIDATES,
                "limit": NEIGHBOURS,
            }
        },
        {
            "$project": {
                "_id": 0,
                "ticket": 1,
                "files_read": 1,
                "files_changed": 1,
                "added_lines": 1,
                "calls": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
        {"$match": {"score": {"$gte": WEAK_SCORE}}},
        {"$sort": {"calls": 1, "score": -1}},
        {"$group": {"_id": None, "runs": {"$push": "$$ROOT"}}},
        {
            "$project": {
                "matched": {"$size": "$runs"},
                "top_score": {"$max": "$runs.score"},
                "lean": {"$slice": ["$runs", {"$ceil": {"$divide": [{"$size": "$runs"}, 2]}}]},
            }
        },
        {"$set": {"n": {"$size": "$lean"}}},
        {
            "$facet": {
                "meta": [
                    {"$project": {"matched": 1, "top_score": 1, "n": 1, "based_on": "$lean.ticket", "calls": "$lean.calls"}}
                ],
                "files_read": _vote("files_read"),
                "files_changed": _vote("files_changed"),
                "lines": _vote("added_lines"),
            }
        },
    ]


class Store:
    def __init__(self, client: MongoClient, db_name: str):
        self.client = client
        self.db = client[db_name]
        self.footprints = self.db["footprints"]
        self.runs = self.db["runs"]

    @classmethod
    def from_env(cls) -> Store | None:
        """Connect using MONGODB_URI from the environment or .env. Returns None when it isn't set."""
        load_dotenv(find_dotenv(usecwd=True))
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            return None
        client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="scope-envelope")
        return cls(client, os.environ.get("MONGODB_DB", "scopeenvelope"))

    # --- setup -------------------------------------------------------------

    def ensure_indexes(self) -> str:
        """Create collections, the runs index and the Vector Search index. Returns the index status."""
        existing = set(self.db.list_collection_names())
        for name in ("footprints", "runs"):
            if name not in existing:
                self.db.create_collection(name)
        self.runs.create_index([("run_id", ASCENDING), ("step", ASCENDING)])
        indexes = {ix["name"]: ix for ix in self.footprints.list_search_indexes()}
        if INDEX_NAME not in indexes:
            self.footprints.create_search_index(
                SearchIndexModel(definition=index_definition(), name=INDEX_NAME, type="vectorSearch")
            )
            return "created"
        return indexes[INDEX_NAME].get("status", "unknown")

    def wait_until_queryable(self, timeout_s: float = 300) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for ix in self.footprints.list_search_indexes(INDEX_NAME):
                if ix.get("queryable"):
                    return True
            time.sleep(5)
        return False

    # --- scope ---------------------------------------------------------------

    def get_scope(self, ticket_text: str, verb: str, subject: str | None) -> Scope:
        docs = list(self.footprints.aggregate(scope_pipeline(ticket_text, verb)))
        return scope_from_facets(docs[0] if docs else None, subject)

    # --- writes --------------------------------------------------------------

    def save_footprint(self, footprint: dict) -> None:
        self.footprints.insert_one({**footprint, "created_at": datetime.now(UTC)})

    def log(self, event: dict) -> None:
        self.runs.insert_one(dict(event))
