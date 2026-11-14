# ScopeEnvelope — Visualization Plan (web viewer, replaces terminal viewer)

Supersedes the terminal-viewer section of `PLAN.md`. Core mechanics (gateway,
agent, `runs`/`footprints` schema in Mongo) are unchanged. Only the
presentation layer changes: Rich terminal tiles → a local web app with two
live animated node-link graphs (lazy vs envelope) + a shared trend chart.

Decisions locked so far: **SSE** (not WebSocket) for server→browser push,
**force-graph** (vasturiano, canvas, CDN, no build step) for the node-link
graphs, **Chart.js** for the trend line chart. Build with a fake data source
first so the visualization can be judged before the Mongo/gateway piece is
ready; the real Mongo Change Stream swaps in later behind the same interface.

---

## How to build this (read first if you have no other context)

Build the milestones in §7 **in order, one at a time, and stop after each
one**. Do not jump ahead or restructure this plan on your own judgment — if
something in it looks wrong once you're in the code, say so and stop instead
of silently deviating.

After finishing each milestone, report back:
1. Which files you created/changed, one line each on what it does.
2. The exact command to run to see/test that milestone (e.g. `python -m
   webviz.mock_source`, then a curl or browser URL).
3. Anything you deviated from in this plan and why.

Then wait to be told to continue before starting the next milestone.
Milestone 3 (frontend against the mock) is the one that needs to actually be
opened in a browser and judged by eye before moving on — don't mark it done
just because the code runs without errors.

---

## 0. Why this order

We build and tune the thing that's highest-risk and most subjective first —
the animation — using a mock data source that doesn't depend on Mongo, the
gateway, or the agent being finished. Everything below is sequenced so each
step produces something you can actually look at before the next step starts.

---

## 1. Data contract (write this down before writing any code)

Everything else — mock source, real source, backend, frontend — depends on
agreeing on one event shape up front, same way `runs`/`footprints` already
anchor the Mongo side.

**Source interface** (already exists for Mongo as `MongoSource.events()` in
`viewer/mongo_source.py` — yields `(event_type, doc)` tuples where
`event_type` is `"run"` or `"footprint"`). The new mock source implements the
exact same interface, so the backend never knows which one it's talking to.

**Wire event** (backend → browser over SSE, one JSON object per message):

```
{
  "condition": "lazy" | "envelope",
  "type": "call" | "blocked" | "done",
  "step": <int>,
  "tool": "read_file" | "write_file" | "list_dir" | "grep" | "run_tests",
  "path": "<file path>" | null,
  "reason": "<why blocked>" | null,
  "in_scope": <bool>,
  "prompt_tokens_cum": <int> | null,
  "cost_usd_cum": <float> | null,
  "tool_calls_cum": <int>
}
```

This is a thin reshaping of what `viewer/core/state.py` already computes from
raw `runs`/`footprints` docs — that module's logic (scope tracking, cumulative
counts, read/write/blocked classification) is reused almost as-is; only its
*output* changes from "mutate a RunState the Rich renderer reads" to "emit one
of these JSON events per call."

**This contract must be a real shared module, not just the prose above.**
Define it once as a `dataclass`/`TypedDict` in `webviz/events.py` — e.g.
`WireEvent` with exactly the fields listed above. `mock_source.py` and
`bridge.py` both import it and build events through it (not by hand-writing
dicts). `app.js` should treat the JSON keys as copied verbatim from that
file. The reason this matters: once mock, backend, and frontend are built
(even by separate passes/agents), there is nothing else forcing the key
names and types to agree — a typo like `cost_usd_cum` vs `costUsd` on one
side will fail silently (frontend just never updates that field) rather than
erroring loudly. One shared module turns a silent drift into an import-time
fact instead of something that only surfaces when you eyeball the chart.

---

## 2. Mock data source (build first, no Mongo involved)

**File:** `webviz/mock_source.py`

- Hardcodes two step sequences (one for `lazy`, one for `envelope`) similar in
  spirit to `viewer/mock_generate.py`'s `STEPS`, but crucially includes at
  least one `blocked` call followed by a *different* path being read/written
  next — this is the case the animation needs to prove out.
- Same `.start()` / `.events()` / `.close()` interface as `MongoSource`, just
  replaying the hardcoded steps on a timer through the same `(event_type,
  doc)` queue shape, instead of reading Mongo. Yields `"run"` docs then one
  `"footprint"` doc per condition, exactly like the real thing.
- Use **uneven** delays between steps (not a constant interval) — real LLM
  tool calls aren't evenly paced, and tuning the animation against perfectly
  smooth fake timing will make it look wrong once real data arrives.
- **Field parity with the real schema is not optional** — this is what makes
  §6's swap a one-line change instead of a rewrite. Each mock `"run"` doc
  must carry exactly the fields `PLAN.md` documents for real `runs` docs:
  `ticket, condition, step, tool, path, decision, reason, prompt_tokens,
  cost_usd, scope_based_on`. Include `prompt_tokens`/`cost_usd` per call in
  the mock *even though the real gateway doesn't populate them per-call yet*
  (PLAN.md gap #1) — the mock represents the target end state, so the
  normalization layer gets built and tested against the full contract now,
  and only needs to tolerate `None` gracefully when pointed at today's real
  data (see §8). Each mock `"footprint"` doc must carry: `ticket, condition,
  calls, prompt_tokens, cost_usd`.
- Runs both conditions back to back in-process (lazy's full sequence, then
  envelope's), so it exercises the "two runs, one after another" shape of the
  orchestrator without needing the agent or gateway at all.

Outcome of this step: a source object you can point the backend at that
behaves exactly like the real one, with deliberately interesting fake data
(including a blocked→reroute moment) to animate against.

---

## 3. Backend: FastAPI + SSE

**Files:** `webviz/server.py`, `webviz/bridge.py`

- `bridge.py`: drains a source's `events()` generator (blocking, runs in a
  background thread — same pattern `MongoSource` already uses internally) and
  pushes normalized wire events (per the contract in §1, built via the
  `webviz/events.py` shared module) onto an `asyncio.Queue` owned by the
  FastAPI event loop.
  - **Thread-safety detail that must not be skipped:** the background thread
    is a different OS thread than the one running the event loop, so you
    cannot call `queue.put_nowait()` directly from it — that races with the
    loop and will drop or corrupt events under real timing even though it
    often looks fine in a quick manual test. Use
    `loop.call_soon_threadsafe(queue.put_nowait, event)` from the thread
    instead, where `loop` is the running loop captured at startup
    (`asyncio.get_event_loop()` from inside the app, passed into the
    bridge).
  - **Consumer count is a deliberate decision, not an oversight:** this is
    built for a single browser tab, single queue, no fan-out and no replay
    buffer. A page refresh or a second tab will not receive past events —
    that's an accepted limitation for a hackathon demo, not something to
    "fix" by building a broadcast/multi-subscriber system. If that
    assumption ever needs to change, it's a deliberate scope increase, not a
    bug fix.
- `server.py`:
  - One `GET /events` endpoint returning a `text/event-stream` response that
    drains the `asyncio.Queue` and yields `data: {json}\n\n` chunks as they
    arrive. (Can use `sse-starlette`'s `EventSourceResponse`, or hand-roll
    with `StreamingResponse` — decide when writing it, both are small.)
  - Serves the static frontend (`webviz/static/`) at `/`.
  - Takes the source (mock or real) as a constructor argument / app-state —
    this is the seam where §2's mock swaps for the real `MongoSource` later
    with no other code changes.
- No business logic lives here beyond relaying — classification/scope/cost
  math stays in the reused `state.py`-derived normalization step.

Outcome: `uvicorn webviz.server:app` serves a page that, if you open it in a
browser and curl/watch `/events`, streams JSON as the mock source plays.

---

## 4. Frontend: two live graphs + counters + trend chart

**Files:** `webviz/static/index.html`, `style.css`, `app.js` (or split further
into `graph.js` / `chart.js` / `sse.js` if it gets big)

- Layout: two graph panels side by side (`lazy` | `envelope`), each with its
  own counters row (tool calls, files read, prompt tokens, cost) underneath
  or alongside; one shared line chart spanning the bottom.
- On load: open `new EventSource('/events')`, dispatch each parsed message by
  `condition` to that side's graph/counters/chart-series.
- Per-graph (force-graph, one instance per condition):
  - First time a `path` is seen for that condition, add a node.
  - `call` with tool `read_file`/`write_file` → color the node (read = blue,
    written = green), and add/keep an edge from the previously-touched node
    in that run to this one, so the graph visibly grows as a path.
  - `blocked` → flash the target node red (and the attempted edge, briefly,
    e.g. drawn then removed/dashed) instead of committing it as a real edge.
  - The *next* successful call after a block draws its edge from the last
    successfully-allowed node, not from the blocked one — this is what reads
    as "it got rejected and went a different way" rather than "a red box
    appeared in a chain."
  - `in_scope` → distinct node border/style (carries over the dashed-border
    "learned scope" idea from the terminal version).
- Chart.js line chart: two series (`lazy`, `envelope`), x = step index, y =
  your pick of `prompt_tokens_cum` or `cost_usd_cum` (maybe a toggle between
  the two, or both as separate charts if there's time) — `chart.data.datasets[i].data.push(...)`
  + `chart.update()` on each relevant event.
- `done` event per condition: freeze that graph/counters, mark status.

Outcome: this is the piece you actually judge. Point it at the mock source,
watch both graphs build, confirm blocked/reroute reads correctly, tune colors/
pacing/edge logic purely against fake data — no Mongo, no agent, no LLM calls
needed to iterate on this.

---

## 5. Orchestrator: one command, two runs, auto-opened browser

**File:** new top-level script, e.g. `run_both.py` (name TBD), or a
`--both`/no-`--condition` mode added to `run.py`.

- Starts the FastAPI server (via `uvicorn.Server(...).serve()` run as a task,
  or a background thread) pointed at whichever source is wired in (mock
  during this phase; real `MongoSource`-backed flow once Mongo/gateway is
  ready).
- Waits until the server is actually accepting connections (poll the port or
  use uvicorn's startup hook) before proceeding — opening the browser too
  early just shows a connection-refused page.
- Calls `webbrowser.open(f"http://localhost:{port}")`.
- Kicks off the two runs sequentially (today: against the mock source, which
  self-plays both conditions; later: `agent.run_ticket(ticket, "lazy")` then
  `agent.run_ticket(ticket, "envelope")`, off the main thread so the server
  loop keeps serving SSE while the agent runs).
- Server keeps running after both finish — process stays alive (Ctrl+C to
  exit) so the page persists in its finished state, matching the requirement
  that the browser tab isn't killed the moment runs complete.

Outcome: `python run_both.py "PIPE-2031: ..."` is the single command — it
opens a browser automatically and you watch both graphs build live, no
separate terminal/server/browser steps.

**This step should work standalone against the mock source before touching
the real agent/Mongo at all** — it's the piece that proves "auto-open browser
+ one command + runs persist after" is actually possible end to end, which is
independent of whether the data behind it is fake or real.

---

## 6. Swapping mock → real (later, once gateway/agent pieces are ready)

- Replace the mock source with `MongoSource` (already written, already has
  the matching interface) in the orchestrator's wiring — ideally a one-line
  change given §2 deliberately matches its interface.
- Confirm the real `runs` docs carry everything §1's contract needs
  (`scope_based_on` for `in_scope`, decision/reason for blocked, per-call
  cumulative tokens — this last one is flagged as not-yet-wired in
  `PLAN.md`'s "known gaps" section and needs to land before the real run can
  drive the trend chart meaningfully).
- No frontend changes expected at this point — it was built against the
  wire-event contract, not against Mongo directly.

---

## 7. Milestones (what "done" looks like at each stage)

1. Mock source alone, printing events to stdout — proves the step sequences
   and timing feel right before any web code exists.
2. Backend + mock source, verified via curling `/events` or a browser SSE
   devtools view — proves the plumbing, no frontend yet.
3. Frontend against §2+§3 — **this is the one worth demoing to yourself** —
   two graphs building live, blocked/reroute visible, trend chart updating.
   Iterate here until it looks right.
4. Orchestrator wrapping §3, still on mock data — one command, auto-browser,
   persists after.
5. Swap in real `MongoSource` + real agent runs once that side is ready.

---

## 8. Open items / risks to flag as we build

- Per-call cumulative token/cost isn't written to `runs` yet by the real
  gateway (see `PLAN.md` gap #1) — doesn't block §1-5 against mock data, but
  the trend chart will be flat/wrong against real data until that's fixed.
- Pacing: real LLM tool calls won't be evenly spaced; the mock source should
  probably use *uneven* delays (not a clean constant) so the animation is
  tuned against something closer to real timing, not an artificially smooth
  demo.
- Exact visual language for "blocked → reroute" (flash duration, dashed vs.
  removed edge, color) is a judgment call best made by eye once §4 exists —
  not worth over-specifying in writing now.
