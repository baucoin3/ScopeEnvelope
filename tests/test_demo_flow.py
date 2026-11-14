"""The demo flow: auto-learn, no self-learning, live diffs, optional checks, and the `agent` command."""

from datetime import UTC, datetime, timedelta

import pytest

from scope_envelope import agent as agent_mod
from scope_envelope import cli, mine
from scope_envelope.config import Config
from scope_envelope.gateway import Gateway
from scope_envelope.scope import Scope
from scope_envelope.store import scope_pipeline
from scope_envelope.tickets import Ticket


class Store:
    def __init__(self, status=None):
        self.status, self.events, self.footprints_saved, self.scope_calls, self.marked = status, [], [], [], []
        self.footprints = self

    # footprints collection stand-in
    def count_documents(self, query):
        return 3

    def get_scope(self, ticket_text, verb, subject, repo=None, before=None, exclude_issue=None):
        self.scope_calls.append({"repo": repo, "exclude_issue": exclude_issue})
        return Scope(confidence="none", subject=subject, matched=3)

    def repo_status(self, repo):
        return self.status

    def mark_learned(self, repo, saved):
        self.marked.append((repo, saved))

    def upsert_footprints(self, fps):
        self.footprints_saved += fps
        return len(fps)

    def ensure_indexes(self):
        return "ready"

    def wait_until_queryable(self, timeout_s=0):
        return True

    def log(self, event):
        self.events.append(event)

    def save_footprint(self, fp):
        self.footprints_saved.append(fp)


TICKET = Ticket(id="acme/app#42", text="Add retry to the uploader", repo="acme/app", number=42,
                created_at=datetime(2026, 11, 1, tzinfo=UTC))


# --- 1. auto-learn -----------------------------------------------------------------

def test_needs_learning():
    now = datetime.now(UTC)
    assert mine.needs_learning(None, 7)
    assert mine.needs_learning({"footprints": 0, "last_learned_at": now}, 7)
    assert mine.needs_learning({"footprints": 5, "last_learned_at": now - timedelta(days=8)}, 7)
    assert not mine.needs_learning({"footprints": 5, "last_learned_at": now - timedelta(days=1)}, 7)
    assert not mine.needs_learning({"footprints": 5, "last_learned_at": now.replace(tzinfo=None)}, 7)  # naive from Mongo


def test_learn_never_ingests_the_tickets_own_fix(monkeypatch):
    seen = {}

    def fake_list(repo, search, limit):
        seen["search"] = search
        return [
            {"number": 1, "title": "Fix 42", "mergedAt": "2026-10-01T00:00:00Z", "changedFiles": 1,
             "closingIssuesReferences": {"nodes": [{"number": 42, "title": "t", "body": ""}]}},
            {"number": 2, "title": "Add retry elsewhere", "mergedAt": "2026-10-02T00:00:00Z", "changedFiles": 1,
             "closingIssuesReferences": {"nodes": [{"number": 7, "title": "t", "body": ""}]}},
        ]

    monkeypatch.setattr(mine, "list_merged_prs", fake_list)
    monkeypatch.setattr(mine, "pr_files", lambda repo, n: [{"filename": "up.py", "patch": "+retry = 3"}])
    store, progress = Store(), []
    report = mine.learn("acme/app", store, before=TICKET.created_at, exclude_issue=42,
                        on_progress=lambda d, t: progress.append((d, t)))
    assert [fp["ticket"] for fp in store.footprints_saved] == ["acme/app#2"]
    assert "merged:<2026-11-01" in seen["search"] and "linked:issue" in seen["search"]
    assert report.saved == 1 and store.marked == [("acme/app", 1)] and progress == [(1, 1)]


def test_auto_learn_skips_when_history_is_recent(monkeypatch):
    monkeypatch.setattr(mine, "learn", lambda *a, **k: pytest.fail("should not learn"))
    store = Store(status={"footprints": 12, "last_learned_at": datetime.now(UTC)})
    result = agent_mod.auto_learn(store, Config(root=".", repo="acme/app"), TICKET, "envelope", "run1")
    assert result == {"learned": False, "reason": "history is recent", "footprints": 12}
    assert store.events[-1]["decision"] == "fresh"


def test_auto_learn_learns_first_when_atlas_has_nothing(monkeypatch):
    calls = {}

    def fake_learn(repo, store, **kwargs):
        calls.update(kwargs)
        kwargs["on_progress"](10, 10)
        return mine.LearnReport(repo=repo, found=10, skipped_large=0, saved=10)

    monkeypatch.setattr(mine, "learn", fake_learn)
    store = Store(status=None)
    result = agent_mod.auto_learn(store, Config(root=".", repo="acme/app", learn_limit=100), TICKET, "envelope", "run1")
    assert result == {"learned": True, "saved": 10}
    assert calls["exclude_issue"] == 42 and calls["before"] == TICKET.created_at and calls["limit"] == 100
    assert [e["decision"] for e in store.events] == ["start", "progress", "done"]
    assert all(e["run_id"] == "run1" and e["tool"] == "learn" for e in store.events)


def test_scope_search_excludes_the_tickets_own_issue():
    vs = scope_pipeline("text", "add", repo="acme/app", exclude_issue=42)[0]["$vectorSearch"]
    assert vs["filter"]["issue"] == {"$ne": 42}


def test_gateway_passes_exclusion_and_records_issue(tmp_path):
    store = Store()
    gw = Gateway("acme/app#42", "Add retry", tmp_path, "envelope", config=Config(root=tmp_path, repo="acme/app"),
                 store=store, exclude_issue=42, run_id="run1", verbose=False)
    assert store.scope_calls[-1] == {"repo": "acme/app", "exclude_issue": 42}
    assert store.events[0]["run_id"] == "run1"


# --- 2. diffs for the browser ------------------------------------------------------------

def test_write_events_carry_a_diff(tmp_path):
    (tmp_path / "up.py").write_text("a = 1\nb = 2\n")
    store = Store()
    gw = Gateway("t", "t", tmp_path, "lazy", config=Config(root=tmp_path), store=store, verbose=False)
    gw.call("write_file", {"path": "up.py", "content": "a = 1\nb = 3\nc = 4\n"})
    event = store.events[-1]
    assert event["change"] == "modified" and event["lines_added"] == 2 and event["lines_removed"] == 1
    assert "-b = 2" in event["diff"] and "+c = 4" in event["diff"]
    gw.call("write_file", {"path": "new.py", "content": "x = 1\n"})
    assert store.events[-1]["change"] == "created" and store.events[-1]["lines_added"] == 1


# --- 3. optional checks -----------------------------------------------------------------

def test_no_checks_and_save_unchecked_saves_marked_unverified(tmp_path):
    store = Store()
    config = Config(root=tmp_path, repo="acme/app", save_unchecked=True)
    gw = Gateway("acme/app#42", "Add retry", tmp_path, "envelope", config=config, store=store,
                 exclude_issue=42, verbose=False)
    summary = gw.finish(gw.run_checks())
    fp = store.footprints_saved[-1]
    assert summary["footprint_saved"] and summary["footprint_reason"] == "saved without checks"
    assert fp["success"] is True and fp["verified"] is False and fp["issue"] == 42


def test_no_checks_without_save_unchecked_saves_nothing(tmp_path):
    store = Store()
    gw = Gateway("t", "t", tmp_path, "lazy", config=Config(root=tmp_path), store=store, verbose=False)
    assert gw.finish(gw.run_checks())["footprint_reason"] == "no checks ran" and not store.footprints_saved


# --- 4. the `agent` command ----------------------------------------------------------------

@pytest.mark.parametrize(("argv", "condition"), [
    (["https://github.com/acme/app/issues/42"], "lazy"),
    (["https://github.com/acme/app/issues/42", "/scopeenvelope"], "envelope"),
    (["/enableScopeEnvelope", "acme/app#42"], "envelope"),
    (["acme/app#42", "--envelope"], "envelope"),
])
def test_agent_command(monkeypatch, argv, condition):
    got = {}
    monkeypatch.setattr(cli, "cmd_run", lambda args: got.update(vars(args)))
    cli.agent_main(argv)
    assert got["condition"] == condition and got["ticket"] in ("https://github.com/acme/app/issues/42", "acme/app#42")
