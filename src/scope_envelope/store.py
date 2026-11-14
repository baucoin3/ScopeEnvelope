"""MongoDB Atlas: footprints (past tickets), runs (live tool calls), and the scope pipeline."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from pymongo import ASCENDING, MongoClient
from pymongo.operations import SearchIndexModel

from .config import load_env
from .scope import WEAK_SCORE, Scope, scope_from_facets

INDEX_NAME = "ticket_idx"
EMBED_MODEL = os.environ.get("SCOPE_EMBED_MODEL", "voyage-4-lite")
CANDIDATES = 100
NEIGHBOURS = 6


def mongo_settings() -> dict:
    """Connection and collection names, from the environment or the nearest .env."""
    load_env()
    return {
        "uri": os.environ.get("MONGODB_URI", ""),
        "db": os.environ.get("MONGODB_DB", "scopeenvelope"),
        "runs": os.environ.get("RUNS_COLLECTION", "runs"),
        "footprints": os.environ.get("FOOTPRINTS_COLLECTION", "footprints"),
    }


def connect(uri: str) -> MongoClient:
    # Short timeouts: a dead connection should fail in seconds, not stall every tool call.
    return MongoClient(
        uri, appname="scope-envelope", serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=15000
    )


def index_definition() -> dict:
    """Atlas Automated Embedding: Atlas embeds `text` itself with a Voyage model."""
    return {
        "fields": [
            {"type": "autoEmbed", "modality": "text", "path": "text", "model": EMBED_MODEL},
            {"type": "filter", "path": "verb"},
            {"type": "filter", "path": "success"},
            {"type": "filter", "path": "repo"},
            {"type": "filter", "path": "merged_at"},
            {"type": "filter", "path": "issue"},
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


def scope_pipeline(
    ticket_text: str,
    verb: str,
    repo: str | None = None,
    before: datetime | None = None,
    exclude_issue: int | None = None,
) -> list[dict]:
    """Similar successful tickets -> cheapest half -> majority vote with provenance.

    `repo` keeps one codebase's history from leaking into another's scope.
    `before` replays a past ticket using only history that existed at the time.
    `exclude_issue` drops history tied to the ticket being worked on, so it never learns its own fix.
    """
    query_filter: dict = {"verb": verb, "success": True}
    if repo:
        query_filter["repo"] = repo
    if before:
        query_filter["merged_at"] = {"$lt": before}
    if exclude_issue is not None:
        query_filter["issue"] = {"$ne": exclude_issue}
    return [
        {
            "$vectorSearch": {
                "index": INDEX_NAME,
                "path": "text",
                "query": {"text": ticket_text},
                "filter": query_filter,
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
                # Agent runs are ranked by tool calls. Mined PRs have no call count, so they
                # rank after agent runs and, among themselves, by how few files they changed.
                "rank": {"$ifNull": ["$calls", {"$add": [1_000_000, {"$size": {"$ifNull": ["$files_changed", []]}}]}]},
            }
        },
        {"$match": {"score": {"$gte": WEAK_SCORE}}},
        {"$sort": {"rank": 1, "score": -1}},
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
    def __init__(self, client: MongoClient, db_name: str, runs: str = "runs", footprints: str = "footprints"):
        self.client = client
        self.db = client[db_name]
        self.footprints = self.db[footprints]
        self.runs = self.db[runs]

    @classmethod
    def from_env(cls) -> Store | None:
        """Connect using MONGODB_URI from the environment or .env. Returns None when it isn't set."""
        settings = mongo_settings()
        if not settings["uri"]:
            return None
        return cls(connect(settings["uri"]), settings["db"], settings["runs"], settings["footprints"])

    # --- setup -------------------------------------------------------------

    def ensure_indexes(self) -> str:
        """Create collections, the runs index and the Vector Search index. Returns the index status."""
        existing = set(self.db.list_collection_names())
        for name in ("footprints", "runs"):
            if name not in existing:
                self.db.create_collection(name)
        self.runs.create_index([("run_id", ASCENDING), ("step", ASCENDING)])
        self.footprints.create_index(
            [("repo", ASCENDING), ("ticket", ASCENDING)],
            name="mined_pr_unique",
            unique=True,
            partialFilterExpression={"source": "github_pr"},
        )
        indexes = {ix["name"]: ix for ix in self.footprints.list_search_indexes()}
        if INDEX_NAME not in indexes:
            self.footprints.create_search_index(
                SearchIndexModel(definition=index_definition(), name=INDEX_NAME, type="vectorSearch")
            )
            return "created"
        current = indexes[INDEX_NAME].get("latestDefinition", {}).get("fields", [])
        if {f.get("path") for f in current} != {f["path"] for f in index_definition()["fields"]}:
            self.footprints.update_search_index(INDEX_NAME, index_definition())
            return "updated"
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

    def get_scope(
        self,
        ticket_text: str,
        verb: str,
        subject: str | None,
        repo: str | None = None,
        before: datetime | None = None,
        exclude_issue: int | None = None,
    ) -> Scope:
        docs = list(self.footprints.aggregate(scope_pipeline(ticket_text, verb, repo, before, exclude_issue)))
        return scope_from_facets(docs[0] if docs else None, subject)

    # --- repo history ----------------------------------------------------------

    def repo_status(self, repo: str) -> dict | None:
        """When this repo's history was last learned, and how many footprints it has."""
        status = self.db["repos"].find_one({"_id": repo})
        if status is not None:
            status["footprints"] = self.footprints.count_documents({"repo": repo, "success": True})
        return status

    def mark_learned(self, repo: str, saved: int) -> None:
        self.db["repos"].update_one(
            {"_id": repo}, {"$set": {"last_learned_at": datetime.now(UTC), "last_saved": saved}}, upsert=True
        )

    # --- writes --------------------------------------------------------------

    def save_footprint(self, footprint: dict) -> None:
        self.footprints.insert_one({**footprint, "created_at": datetime.now(UTC)})

    def upsert_footprints(self, footprints: list[dict]) -> int:
        """Insert or replace mined footprints, keyed by (repo, ticket), so `learn` can be re-run."""
        now = datetime.now(UTC)
        for fp in footprints:
            self.footprints.replace_one(
                {"source": "github_pr", "repo": fp["repo"], "ticket": fp["ticket"]},
                {**fp, "source": "github_pr", "created_at": now},
                upsert=True,
            )
        return len(footprints)

    def log(self, event: dict) -> None:
        self.runs.insert_one(dict(event))
