# ScopeEnvelope

A gateway for coding agents. It learns each ticket type's footprint from the repo's own past work (merged PRs and their tickets, stored in MongoDB Atlas), tells the agent that scope up front, and blocks writes outside it. A terminal map shows each run live.

## Install

```bash
uv sync --all-extras          # or: pip install -e '.[agent,viewer]'
cp .env.example .env          # MONGODB_URI, OPENROUTER_API_KEY, MODEL
```

Extras: `agent` (LangChain agent), `viewer` (terminal map). The gateway itself only needs `pymongo` and `python-dotenv`.

## Use it on any repo

```bash
cd path/to/your-repo
scope-envelope init       # writes .scope-envelope.toml: repo (from the git remote) and test command (from go.mod, package.json, ...)
scope-envelope setup      # Atlas collections + Vector Search index (Automated Embedding, no embedding API needed)
scope-envelope learn      # mine merged PRs that close a ticket into footprints
scope-envelope scope "r/aws_sns_topic: Add foo argument"    # see what it learned, with provenance
```

Run a ticket, with the live map in a second terminal:

```bash
scope-envelope view owner/repo#123 --condition envelope     # terminal 1, start first
scope-envelope run https://github.com/owner/repo/issues/123 --condition envelope   # terminal 2
```

`--condition lazy` runs the same agent with no scope (observe only), for the side-by-side comparison.

## .scope-envelope.toml

Every key is optional; `init` fills in what it can detect.

| Key | Meaning |
|---|---|
| `repo` | GitHub `owner/name` whose history to learn from |
| `test_command` | how to check a change, e.g. `go build ./...` (empty = no check, so nothing is saved as history) |
| `subject_patterns` | extra regexes whose first group names a ticket's subject, e.g. `'\bacme_([a-z_]+)'` |
| `approval` | what happens on `request_write`: `auto`, `deny` or `prompt` |
| `model` | OpenRouter model id (default `deepseek/deepseek-v4-pro`); `MODEL` in the environment overrides it |
| `max_calls` | tool-call limit per run |

## Using the gateway from your own agent

```python
from scope_envelope import Gateway

gw = Gateway("owner/repo#123", ticket_text, repo_root=".", mode="envelope")
system_prompt = BASE_PROMPT + gw.scope_prompt()       # "" in lazy mode
tools = gw.tools()                                    # LangChain tools routed through the gateway
gw.record_llm_step(prompt_tokens, completion_tokens, cost_usd, "openrouter_billed")   # after every model reply
summary = gw.finish(gw.run_checks())                  # runs test_command; saves the footprint if it passed
```

Tools: `list_dir`, `read_file`, `grep`, `write_file`, `run_tests`, `request_write`. File listing and search follow `git ls-files`, so `.gitignore` is respected. If Atlas is unreachable, tool calls keep working and telemetry is skipped with a warning.

## What is stored in Atlas

- `footprints`: one document per learned ticket (mined PR or passing agent run): ticket text, normalized files changed and lines added, calls, tokens, billed cost, model, checks.
- `runs`: one event per tool call and per model reply, with running totals. The viewer reads these through a Change Stream.

## Layout

```
src/scope_envelope/
  config.py      per-repo settings (.scope-envelope.toml)
  normalize.py   subject names and numbers -> placeholders, so history applies to new tickets
  store.py       Atlas collections, Vector Search index, the scope aggregation pipeline
  scope.py       the learned scope, confidence levels, prompt text
  gateway.py     allow/block decisions, tool execution, runs logging, footprints
  mine.py        merged PR + linked ticket -> footprint
  tickets.py     ticket link -> ticket text
  agent.py       LangChain agent on OpenRouter, reports billed cost per step
  tools.py       LangChain tool wrappers
  viewer/        terminal map (rich) fed by the runs Change Stream
  cli.py         scope-envelope command
tests/           unit tests; tests/fixtures/toy_repo is a tiny repo for smoke runs
docs/            plan and visual reference
```
