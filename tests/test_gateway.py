import pytest

from scope_envelope.gateway import Gateway, extract_paths, extract_verb
from scope_envelope.scope import Learned, Scope, render_prompt, scope_from_facets

TICKET = "Implement Kafka consumer for OrderPlacedEvent"


class FakeStore:
    def __init__(self, scope=None):
        self.scope = scope
        self.footprints = []
        self.events = []

    def get_scope(self, ticket_text, verb, subject, repo=None, before=None):
        if self.scope is not None:
            self.scope.subject = subject
        return self.scope

    def save_footprint(self, footprint):
        self.footprints.append(footprint)

    def log(self, event):
        self.events.append(event)


def learned_scope():
    return Scope(
        confidence="strong",
        subject=None,
        files_read=[Learned("pipeline/consumers/user_activity_consumer.py", ["PIPE-1801", "PIPE-1805"])],
        files_changed=[
            Learned("pipeline/consumers/{snake}_consumer.py", ["PIPE-1801", "PIPE-1805"]),
            Learned("pipeline/registry.py", ["PIPE-1801", "PIPE-1805"]),
        ],
        lines=[Learned("enable_{snake}_consumer: false", ["PIPE-1801", "PIPE-1805"])],
        based_on=["PIPE-1801", "PIPE-1805"],
        median_calls=4,
        call_budget=6,
        matched=2,
        top_score=0.9,
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pipeline" / "consumers").mkdir(parents=True)
    (tmp_path / "pipeline" / "registry.py").write_text("CONSUMERS = []\n")
    (tmp_path / "pipeline" / "consumers" / "user_activity_consumer.py").write_text("class UserActivityConsumer: ...\n")
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "strings.py").write_text("def slug(s): return s\n")
    return tmp_path


def make(repo, mode="envelope", scope=None, approver=None, ticket=TICKET):
    store = FakeStore(scope)
    return Gateway("PIPE-2031", ticket, repo, mode, store=store, approver=approver, verbose=False), store


def test_lazy_mode_allows_any_write_and_injects_nothing(repo):
    gw, _ = make(repo, mode="lazy", scope=learned_scope())
    assert gw.scope_prompt() == ""
    assert gw.call("write_file", {"path": "utils/strings.py", "content": "x\n"}) == "Wrote utils/strings.py"


def test_scoped_write_inside_scope_is_allowed(repo):
    gw, _ = make(repo, scope=learned_scope())
    result = gw.call("write_file", {"path": "pipeline/consumers/order_placed_consumer.py", "content": "pass\n"})
    assert result.startswith("Wrote")
    assert (repo / "pipeline/consumers/order_placed_consumer.py").exists()


def test_scoped_write_outside_scope_is_blocked_and_not_counted(repo):
    gw, store = make(repo, scope=learned_scope())
    result = gw.call("write_file", {"path": "utils/strings.py", "content": "broken\n"})
    assert result.startswith("Blocked: utils/strings.py")
    assert "pipeline/registry.py" in result
    assert (repo / "utils/strings.py").read_text() == "def slug(s): return s\n"
    assert gw.calls == 0 and gw.blocked == 1
    assert store.events[-1]["decision"] == "blocked"


def test_request_write_approval_unblocks_the_file(repo):
    gw, _ = make(repo, scope=learned_scope(), approver=lambda p, r: (True, "ok"))
    assert gw.call("request_write", {"path": "migrations/0042_orders.py", "reason": "needs a table"}).startswith("Approved")
    assert gw.call("write_file", {"path": "migrations/0042_orders.py", "content": "x\n"}).startswith("Wrote")


def test_request_write_denied_stays_blocked(repo):
    gw, _ = make(repo, scope=learned_scope(), approver=lambda p, r: (False, "no"))
    assert gw.call("request_write", {"path": "utils/strings.py", "reason": "tidy"}).startswith("Denied")
    assert gw.call("write_file", {"path": "utils/strings.py", "content": "x"}).startswith("Blocked")


def test_paths_named_in_ticket_are_allowed(repo):
    gw, _ = make(repo, scope=learned_scope(), ticket=TICKET + " and add a migration under migrations/")
    assert gw.call("write_file", {"path": "migrations/0042_orders.py", "content": "x\n"}).startswith("Wrote")


def test_no_scope_means_writes_need_approval(repo):
    gw, _ = make(repo, scope=Scope(confidence="none", subject=None))
    assert "request_write" in gw.scope_prompt()
    assert gw.call("write_file", {"path": "pipeline/registry.py", "content": "x"}).startswith("Blocked")


def test_paths_outside_repo_are_rejected(repo):
    gw, _ = make(repo, mode="lazy")
    assert gw.call("read_file", {"path": "../../etc/passwd"}).startswith("Error: path is outside the repo")


def test_unknown_tool_is_an_error(repo):
    gw, _ = make(repo, mode="lazy")
    assert gw.call("bash", {"cmd": "rm -rf /"}).startswith("Error: unknown tool")


def test_reads_and_grep(repo):
    gw, _ = make(repo, mode="lazy")
    assert "consumers/" in gw.call("list_dir", {"path": "pipeline"})
    assert "UserActivityConsumer" in gw.call("read_file", {"path": "pipeline/consumers/user_activity_consumer.py"})
    assert "pipeline/registry.py:1:" in gw.call("grep", {"pattern": "CONSUMERS", "path": "."})
    assert gw.files_read == ["pipeline/consumers/user_activity_consumer.py"]


def test_budget_warning_after_expected_calls(repo):
    gw, _ = make(repo, scope=learned_scope())
    for _ in range(5):
        gw.call("list_dir", {"path": "."})
    assert "expected tool calls used" in gw.call("list_dir", {"path": "."})


def test_finish_saves_normalized_footprint_and_reports_lines(repo):
    gw, store = make(repo, scope=learned_scope())
    gw.call("read_file", {"path": "utils/strings.py"})
    gw.call("write_file", {
        "path": "pipeline/consumers/order_placed_consumer.py",
        "content": "class OrderPlacedConsumer:\n    group_id = 'integration-order-placed'\n",
    })
    gw.call("write_file", {"path": "pipeline/registry.py", "content": "CONSUMERS = []\nenable_order_placed_consumer: false\n"})
    gw.record_llm_step(prompt_tokens=1000, completion_tokens=50, cost_usd=0.001)
    summary = gw.finish({"passed": 10, "total": 10})

    fp = store.footprints[0]
    assert fp["success"] is True
    assert fp["files_changed"] == ["pipeline/consumers/{snake}_consumer.py", "pipeline/registry.py"]
    assert "group_id = 'integration-{dashed}'" in fp["added_lines"]
    assert "enable_{snake}_consumer: false" in fp["added_lines"]
    assert summary["lines_confirmed"] == 1 and summary["lines_expected"] == 1
    assert summary["noise_files"] == 1  # utils/strings.py
    assert summary["prompt_tokens"] == 1000
    assert summary["footprint_saved"] is True
    assert store.events[-1]["tool"] == "finish"


def test_failed_run_is_not_saved(repo):
    gw, store = make(repo, mode="lazy")
    summary = gw.finish({"passed": 7, "total": 10})
    assert store.footprints == [] and summary["footprint_reason"] == "checks failed"


def test_finish_twice_raises(repo):
    gw, _ = make(repo, mode="lazy")
    gw.finish({"passed": 1, "total": 1})
    with pytest.raises(RuntimeError):
        gw.finish({"passed": 1, "total": 1})


def test_langchain_tools_route_through_gateway(repo):
    gw, store = make(repo, scope=learned_scope())
    tools = {t.name: t for t in gw.tools()}
    assert set(tools) == {"list_dir", "read_file", "grep", "write_file", "run_tests", "request_write"}
    assert tools["write_file"].invoke({"path": "utils/strings.py", "content": "x"}).startswith("Blocked")
    assert store.events[-1]["decision"] == "blocked"


def test_extract_helpers():
    assert extract_verb("Implement Kafka consumer") == "add"
    assert extract_verb("Remove the legacy consumer") == "delete"
    assert extract_paths("Edit `pipeline/registry.py` and/or add files in migrations/.") == [
        "pipeline/registry.py",
        "migrations/",
    ]


def test_scope_from_facets_confidence_levels():
    def facets(score, n):
        return {
            "meta": [{"matched": n, "top_score": score, "n": n, "based_on": ["A", "B"][:n], "calls": [20, 24][:n]}],
            "files_read": [], "files_changed": [{"_id": "x.py", "from": ["A", "B"]}], "lines": [],
        }

    strong = scope_from_facets(facets(0.9, 2), "order placed")
    assert strong.confidence == "strong" and strong.call_budget == 24  # median 22 + 2
    assert scope_from_facets(facets(0.7, 2), None).confidence == "weak"
    assert scope_from_facets(facets(0.9, 1), None).confidence == "weak"  # one run isn't enough for strong
    assert scope_from_facets(facets(0.5, 2), None).confidence == "none"
    assert scope_from_facets(None, None).confidence == "none"


def test_render_prompt_lists_concrete_files():
    scope = learned_scope()
    scope.subject = "order placed"
    text = render_prompt(scope)
    assert "learned from PIPE-1801, PIPE-1805" in text
    assert "pipeline/consumers/order_placed_consumer.py" in text
    assert "enable_order_placed_consumer: false" in text
