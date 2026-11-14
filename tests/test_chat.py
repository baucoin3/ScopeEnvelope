from scope_envelope.chat import compare, event_line, parse

LINK = "https://github.com/hashicorp/terraform-provider-aws/issues/39618"


def test_parse_intents():
    assert parse(f"Please take a look at this ticket {LINK}") == ("run", LINK, "Please take a look at this ticket")
    assert parse("Enable ScopeEnvelope Gateway")[0] == "on"
    assert parse("/scopeenvelope on")[0] == "on"
    assert parse("disable the scope envelope gateway")[0] == "off"
    assert parse("/scopeenvelope off")[0] == "off"
    assert parse("/exit")[0] == "quit"
    assert parse("hello")[0] == "chat"
    assert parse("work on acme/app#12")[1] == "acme/app#12"


def test_event_lines():
    assert "blocked" in event_line({"tool": "write_file", "decision": "blocked", "path": "x.go", "reason": "outside"})
    assert "+3/-1" in event_line({"tool": "write_file", "decision": "written", "path": "x.go", "lines_added": 3, "lines_removed": 1})
    assert "learned 40/97" in event_line({"tool": "learn", "decision": "progress", "done": 40, "total": 97})
    assert event_line({"tool": "llm"}) is None


def test_compare():
    lazy = {"condition": "lazy", "calls": 40, "prompt_tokens": 80000, "cost_usd": 0.08, "files_read": 20}
    env = {"condition": "envelope", "calls": 20, "prompt_tokens": 40000, "cost_usd": 0.04, "files_read": 10}
    assert compare(env, lazy) == "vs lazy: -50% calls, -50% prompt tokens, -50% cost, -50% files read"
