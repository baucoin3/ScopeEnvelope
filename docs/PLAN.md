# ScopeEnvelope — Phase 1 Plan

Hackathon project (Agents in Context Toronto, Nov 14 2026, team of 3). This supersedes any earlier note that said MongoDB/Vector Search was cut — it is not. The only thing changing from `idea.md` is the visualization layer: web dashboard → terminal.

## Core idea (unchanged from idea.md)

MongoDB Atlas is the core. A gateway learns each ticket type's footprint from past successful work, uses Atlas Vector Search to retrieve similar past tickets, computes a scope (files to read/change, conventions, call budget, provenance), injects it into the agent, and blocks writes outside it. Demo compares `lazy` (no scope) vs `envelope` (learned scope) on the same ticket.

## What's actually changing

Only the visualization: no web dashboard. A **terminal viewer** shows the same information the heatmap would — files touched (read/written/blocked/untouched), live call count, tokens, cost — live, ideally side-by-side for `lazy` vs `envelope` runs on the same ticket.

## Components

| Part | Owner | Notes |
|---|---|---|
| Gateway + Atlas | Rami | Footprint extraction, normalization, embeddings, Vector Search index, scope pipeline w/ provenance, scope injection, write-blocking, cold-start approval, writes to `runs` |
| Agent | Teammate | LangChain `create_agent`, 5 tools (`list_dir`, `read_file`, `grep`, `write_file`, `run_tests`), all calls go through `gateway.call(tool, args)` |
| Terminal viewer | Us | Reads live run data, renders it in-terminal instead of a web heatmap |
| Toy repo + tickets | Us | Small repo + Jira-style tickets to run against |

## Open decision: how the terminal viewer gets its data

Two options, need to lock one before building:

1. **MongoDB Change Streams on `runs`** (matches idea.md's architecture directly — `runs` already logs every tool call for the UI). Gateway writes each call to Atlas; viewer opens a change stream and renders as events arrive. One source of truth, no second transport. Requires Atlas (replica set), which we're already on.
2. **JSONL file tail** as a local fallback — zero network dependency, survives Atlas flakiness mid-demo, but duplicates state outside Mongo.

**Leaning: Change Streams as primary**, since Mongo is already the core and `runs` already exists in the data model. Keep file-tail only as a demo-safety fallback if Atlas connectivity is a risk Saturday.

## `runs` document shape (from idea.md, unchanged)

```
{ ticket, condition, step, tool, path, decision, reason, prompt_tokens, cost_usd, scope_based_on }
```

## Build order

1. Confirm Atlas connection + Vector Search index works end to end (minimal: insert a footprint, query it back).
2. Toy repo + 5 tools + stub gateway (pass-through, scope = {}) to get the agent loop running.
3. Wire the real gateway: `get_scope`, `gateway.call`, `save_footprint` against Atlas.
4. Gateway writes every tool call to `runs`.
5. Terminal viewer opens a Change Stream on `runs`, renders files touched / calls / tokens / cost live.
6. Run `lazy` vs `envelope` on the same ticket, compare in two terminal panes or sequential replay.

## Open questions carried over from idea.md

- Side-by-side terminal layout for two runs: two panes/split, or sequential replay of a recorded run?
- Cost: computed from token price, or OpenRouter's billed amount?
- Terminal library: `rich` (simpler, good enough for a live table/tree) vs `textual` (if we want true side-by-side panes).

## Known gaps after wiring agent -> Mongo (2026-11-14)

`agent.py`/`stub_gateway.py` now write to `runs` and `footprints`, but three things are still stubbed or missing before this is real:

1. **`runs` docs are missing `prompt_tokens`/`cost_usd` per call.** The documented shape (above) has them; the current insert in `stub_gateway.call_tool` doesn't. Token usage is only known from `usage_metadata` on each AIMessage, which arrives *before* the tool call it triggers — so the fix is a LangChain callback (or manual message-walk) that updates a running cumulative total in `_run_state` as each LLM step completes, and `_call_tool` attaches that cumulative snapshot to the `runs` doc it writes. `cost_usd` additionally needs a price-per-token table for whatever `MODEL` is set to (OpenRouter publishes per-model pricing) — doesn't exist yet, currently hardcoded `None` in the `footprints` write too.

2. **Mongo writes in `call_tool`/`save_footprint` have no error handling.** A bad connection or auth failure currently raises out of `insert_one` and crashes the tool call — i.e. kills the actual coding run, not just the telemetry. Needed: wrap both inserts in try/except, log a structured message (collection, ticket, step, tool, exception type + message) via `logging` rather than crashing, and let the tool call's real result still return to the agent on failure. Also set a short `serverSelectionTimeoutMS` on the `MongoClient` (default ~30s) so a dead connection fails fast instead of stalling each of up to `RECURSION_LIMIT` (15) tool calls for up to 30s apiece.

3. **Scope enforcement doesn't exist yet — `lazy` and `envelope` are currently behaviorally identical.** `get_scope` always returns `[]` and `call_tool` always writes `decision: "allowed"`. Two halves needed:
   - *Computing* a real scope (Rami's piece — Atlas Vector Search over embedded past tickets/footprints). Cold-start problem: Atlas has no history yet, so real vector search returns nothing for a while. Need a fallback decided before the demo depends on it — either seed a few tickets first to build `footprints` history, or fall back to a cheap heuristic (e.g. keyword/path match against the toy repo) so `envelope` has a non-empty scope to enforce even on the first ticket of a type.
   - *Enforcing* it — in `call_tool`, compare `write_file`'s `path` against the scope computed for this run (cached once per `ticket`/`condition`, not re-fetched every call) and set `decision: "blocked"`, `reason: "outside scope"` when it's outside, only when `condition == "envelope"`. Reads stay unrestricted per idea.md — scope governs writes, not reads.
