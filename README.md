# ScopeEnvelope

A gateway for coding agents. It learns each ticket type's footprint from past successful runs (stored in MongoDB Atlas), tells the agent that scope up front, and blocks writes outside it.

## Setup

```bash
uv sync
cp .env.example .env   # then fill in MONGODB_URI
uv run python scripts/check_atlas.py    # connection check
uv run python scripts/setup_atlas.py    # collections + Vector Search index (Automated Embedding)
uv run pytest -q
```

Without `MONGODB_URI` the gateway still works: it runs and logs tool calls but has no scope and saves nothing.

## Using the gateway from an agent

```python
from scope_envelope import Gateway

gw = Gateway(ticket_id="PIPE-2031", ticket_text="Implement Kafka consumer for OrderPlacedEvent ...",
             repo_root="work/PIPE-2031-scoped/", mode="scoped")   # or "raw"

system_prompt = BASE_PROMPT + gw.scope_prompt()   # "" in raw mode
tools = gw.tools()                                # LangChain tools routed through the gateway

gw.record_llm_step(prompt_tokens=..., completion_tokens=..., cost_usd=...)   # after every model reply
summary = gw.finish(checks={"passed": 10, "total": 10})                      # saves the footprint if it passed
```

Tools: `list_dir`, `read_file`, `grep`, `write_file`, `run_tests`, `request_write`.

`SCOPE_APPROVAL` controls `request_write`: `auto` (default, approve and flag), `deny`, or `prompt` (ask in the terminal).

## Layout

- `normalize.py`: subject names (snake, dashed, class, ...) and migration numbers to placeholders
- `store.py`: Atlas collections, Vector Search index, the scope aggregation pipeline
- `scope.py`: the learned scope, confidence levels, prompt text
- `gateway.py`: allow/block decisions, tool execution, `runs` logging, footprints
- `tools.py`: LangChain tool wrappers
