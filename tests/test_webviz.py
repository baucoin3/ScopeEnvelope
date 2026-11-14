"""The browser feed: real gateway events -> the WireEvents app.js draws."""

from scope_envelope.gateway import Gateway
from scope_envelope.scope import Learned, Scope
from scope_envelope.webviz.events import ConditionNormalizer


class ListStore:
    def __init__(self, scope):
        self.scope, self.events = scope, []

    def get_scope(self, ticket_text, verb, subject, **kwargs):
        return self.scope

    def log(self, event):
        self.events.append(event)

    def save_footprint(self, fp):
        pass


def test_live_events_become_browser_events(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "utils.py").write_text("y = 1\n")
    scope = Scope(confidence="strong", subject=None, based_on=["a#1"], call_budget=9, median_calls=5,
                  files_changed=[Learned("app.py", ["a#1"])])
    store = ListStore(scope)
    gw = Gateway("a#2", "Fix app", tmp_path, "envelope", store=store, verbose=False)
    gw.record_llm_step(1000, 10, 0.001, "openrouter_billed")
    gw.call("list_dir", {"path": "."})
    gw.call("read_file", {"path": "utils.py"})
    gw.call("write_file", {"path": "utils.py", "content": "y = 2\n"})
    gw.call("write_file", {"path": "app.py", "content": "x = 2\n"})
    gw.finish({"passed": 0, "total": 0})

    n = ConditionNormalizer("a#2", "envelope")
    wire = [n.normalize("run", e).to_dict() for e in store.events]
    assert wire[0]["type"] == "start"
    by_tool = {(w["tool"], w["type"]): w for w in wire}
    assert by_tool[("list_dir", "call")]["path"] is None  # folders stay counters-only
    assert by_tool[("read_file", "call")]["path"] == "utils.py"
    assert by_tool[("write_file", "blocked")]["path"] == "utils.py"
    written = by_tool[("write_file", "call")]
    assert written["path"] == "app.py" and written["in_scope"] and "+x = 2" in written["diff"]
    assert wire[-1]["type"] == "done" and wire[-1]["tool_calls_cum"] == 3
    assert wire[-1]["prompt_tokens_cum"] == 1000 and wire[-1]["cost_usd_cum"] == 0.001


def test_local_source_delivers_published_events():
    from scope_envelope.webviz.local_source import LocalSource, publish

    publish({"tool": "read_file", "path": "a.py", "condition": "lazy"})
    event_type, doc = next(LocalSource().events())
    assert event_type == "run" and doc["path"] == "a.py"
