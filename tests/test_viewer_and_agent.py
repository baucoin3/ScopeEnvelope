"""Viewer state fed by real gateway events, cost reporting, and ticket links. No network, no Atlas."""

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from scope_envelope.agent import UsageReporter, price
from scope_envelope.gateway import Gateway
from scope_envelope.scope import Learned, Scope
from scope_envelope.tickets import fetch_ticket, parse_ref
from scope_envelope.viewer.core import render
from scope_envelope.viewer.core import state as viewer_state


class ListStore:
    def __init__(self, scope=None):
        self.scope, self.events = scope, []

    def get_scope(self, ticket_text, verb, subject, repo=None, before=None, **kwargs):
        if self.scope:
            self.scope.subject = subject
        return self.scope

    def log(self, event):
        self.events.append(event)

    def save_footprint(self, fp):
        pass


def scripted_run(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "utils.py").write_text("y = 2\n")
    scope = Scope(
        confidence="strong", subject=None, based_on=["acme/app#3"], call_budget=10, median_calls=5,
        files_changed=[Learned("app.py", ["acme/app#3"])],
    )
    store = ListStore(scope)
    gw = Gateway("acme/app#9", "Fix the app", tmp_path, "envelope", store=store, verbose=False)
    gw.record_llm_step(1200, 40, 0.0011, "openrouter_billed")
    gw.call("read_file", {"path": "utils.py"})
    gw.call("write_file", {"path": "utils.py", "content": "y = 3\n"})  # blocked
    gw.call("write_file", {"path": "app.py", "content": "x = 2\n"})
    gw.finish({"passed": 1, "total": 1})
    return store.events


def test_viewer_state_from_gateway_events(tmp_path):
    state = viewer_state.new_state("acme/app#9", "envelope")
    for event in scripted_run(tmp_path):
        viewer_state.apply_run_event(state, event)
    assert state.status == viewer_state.DONE
    assert state.tiles == {"utils.py": viewer_state.BLOCKED, "app.py": viewer_state.WRITTEN}
    assert state.scope == {"app.py"} and state.based_on == ["acme/app#3"]
    assert state.tool_calls == 2 and state.blocked == 1  # llm/scope/finish events and the blocked write don't count
    assert state.prompt_tokens == 1200 and state.token_steps == [1200]
    assert state.summary["checks"] == {"passed": 1, "total": 1}
    render.build_renderable(state)  # renders without errors


def llm_result(token_usage=None, usage_metadata=None):
    meta = {**usage_metadata, "total_tokens": sum(usage_metadata.values())} if usage_metadata else None
    message = AIMessage(content="ok", usage_metadata=meta)
    return LLMResult(generations=[[ChatGeneration(message=message)]], llm_output={"token_usage": token_usage or {}})


def test_usage_reporter_prefers_openrouter_billed_cost(tmp_path):
    gw = Gateway("t", "t", tmp_path, "lazy", store=None, model="deepseek/deepseek-v4-pro", verbose=False)
    UsageReporter(gw).on_llm_end(llm_result({"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.0012}))
    assert (gw.prompt_tokens, gw.completion_tokens, gw.cost_usd) == (1000, 100, 0.0012)
    assert gw.cost_sources == {"openrouter_billed"}


def test_usage_reporter_falls_back_to_price_table(tmp_path):
    gw = Gateway("t", "t", tmp_path, "lazy", store=None, model="deepseek/deepseek-v4-pro", verbose=False)
    UsageReporter(gw).on_llm_end(llm_result(usage_metadata={"input_tokens": 1_000_000, "output_tokens": 0}))
    assert gw.prompt_tokens == 1_000_000 and gw.cost_usd == 0.90
    assert gw.cost_sources == {"price_table"}


def test_price_unknown_model():
    assert price("someone/unknown", 10, 10) is None


def test_ticket_refs():
    assert parse_ref("https://github.com/hashicorp/terraform-provider-aws/issues/50996") == (
        "hashicorp/terraform-provider-aws", 50996
    )
    assert parse_ref("acme/app#12") == ("acme/app", 12)
    assert parse_ref("Fix the login bug") is None


def test_plain_text_ticket_needs_no_network():
    ticket = fetch_ticket("  Fix the login bug  ")
    assert ticket.id.startswith("ticket-") and ticket.text == "Fix the login bug"
    assert ticket.number is None and ticket.created_at is None
