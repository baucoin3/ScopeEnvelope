# ScopeEnvelope Web Viewer — Visual Design Context

This is not the build plan (`visualization-plan.md` covers that — milestones,
file layout, decisions). This file exists so a fresh Claude instance (or a
human) can understand *what the visualization is trying to show and why*,
independent of how it was built, and compare the current implementation
against that intent.

## The demo's actual point

ScopeEnvelope compares two coding-agent runs on the *same ticket*: `lazy`
(no guidance — the agent explores and edits however it wants) vs `envelope`
(the agent is given a learned scope — which files it should touch — and
writes outside that scope get blocked). The visualization's whole job is to
make that contrast *visible and legible in real time*, not just reportable
as a final number. Two things should be obvious to someone watching cold,
with no narration:

1. **Envelope stays disciplined, lazy wanders.** Envelope's graph should
   look tight and contained (clustered around a small set of in-scope
   files); lazy's graph should look sprawling (touches more files, no
   boundary).
2. **Envelope actively rejects bad moves.** At some point envelope should
   try to write somewhere it shouldn't, get visibly blocked, and then
   correctly continue work elsewhere — proving the system isn't just
   "lucky," it's *enforcing* something.

Everything else (cost, tokens, call counts) is supporting evidence for "the
discipline was cheaper," not the headline.

## Screen layout

Two side-by-side panels, `lazy` | `envelope`, each a force-directed node
graph with a counters row (tool calls / files read / tokens / cost),
followed by one shared line chart spanning the bottom comparing the two
conditions over time (tokens or cost, toggle between them).

## The graph: what a node/edge is and what it means

- **A node is a file path.** It appears the first time any tool call
  touches that path. Untouched files in the repo never appear — the graph
  only ever shows the files that were actually part of the story.
- **An edge connects consecutively-touched files within one run** — i.e.
  "the agent went from touching file A to touching file B next." It's a
  path/trail, not a dependency graph. The edges are what make the layout
  read as "a sequence of exploration," not a static map.
- **Node color is the file's current state:**
  - blue = read
  - green = written
  - red = blocked (attempted write, rejected)
  - A node's color only ever escalates read → written/blocked, never
    downgrades back to read once it's been written.
- **Dashed border = "the system told the agent this file was in scope."**
  This is the visual tell for envelope's discipline — lazy should never
  show this border (it has no scope), envelope should show it on the
  handful of files it was actually allowed to touch.
- **The blocked → reroute moment** (the single most important beat in the
  whole visualization): when a write is blocked, the target node flashes
  red and a dashed/temporary edge briefly appears showing the attempted
  path, then disappears — it never becomes a permanent edge. The *next*
  successful write's edge must originate from the last node that was
  *actually allowed*, not from the blocked node. This is what makes it
  read as "rejected, and the agent correctly moved on" rather than "a red
  box appeared in an otherwise normal chain." If this reads ambiguously,
  it's the first thing to fix.
- **Both graphs settle into a final layout and stay interactive-looking**
  (gentle physics, not frozen mid-motion) once their run finishes.

## The trend chart: what it's proving

X axis = step number, Y axis = cumulative prompt tokens or cumulative cost
(toggle), one line per condition. The two lines should *diverge* over the
course of the run, not just land at different final numbers — lazy's line
climbing faster/higher than envelope's, call by call, is what sells "the
discipline was cheaper the whole time," not just "cheaper at the end."
A line that's flat for a while and then jumps once at the very end reads as
broken, not as "efficient."

## Why this uses two live source conditions, not a replay

The whole thing is built to watch a *live* run and animate as it happens —
not generate the animation from a finished log. That's why the system is an
event stream (SSE) from day one instead of, say, rendering a fixed
JSON trace. The "live-ness" itself is part of the pitch: this is what it
looks like while the agent is actually working, not a post-hoc chart.

## What drives this today (mock vs. real)

The frontend only ever sees one JSON event shape (`webviz/events.py`'s
`WireEvent`) over `/events`. It does not know or care whether that event
originated from a hardcoded mock sequence or an actual agent run writing to
MongoDB — same contract either way. Right now (`webviz/server.py`) it's
wired to the mock, specifically so the above visual language can be tuned
by eye without needing real Mongo credentials or spending real LLM tokens
per iteration.

**What the mock fabricates to prove out the visual language above:**
- One deliberate blocked write (envelope trying to write `test_app.py`,
  outside its scope of `app.py`/`utils.py`) followed immediately by a
  successful write back to `app.py` — this is the only place the
  blocked → reroute moment currently fires.
- A hardcoded scope (`["app.py", "utils.py"]`) applied only to envelope,
  which is what currently puts the dashed border on those two nodes.
- Deliberately larger token/cost numbers for lazy than envelope at every
  step, so the trend chart visibly diverges throughout the run, not just
  at the end.
- Irregular (not evenly-spaced) delays between steps, so the animation
  isn't tuned against a metronome that won't match real LLM call timing.

**The honest caveat:** none of those three bullets reflect something the
real gateway can do yet. The real gateway currently allows every write
unconditionally and doesn't compute a scope, so *today*, pointed at real
data, you would see: no blocked calls ever, no dashed borders ever, and a
flat trend line that jumps once at the very end when the final token count
arrives. Tuning the visuals against the mock is still the right move — the
rendering code is 100% identical either way — but know that these specific
three behaviors are currently illustrating a target state, not a
reproduction of what real data does today.

## Judging the current build against this intent

When comparing an implementation to this doc, the questions that matter
are:
1. Does the blocked → reroute sequence read unambiguously as "rejected,
   then correctly continued elsewhere" within roughly a second of it
   happening, without needing it explained?
2. Does envelope's graph *look* visibly tighter/more contained than lazy's,
   at a glance, without reading any numbers?
3. Does the trend chart show divergence building over time, or just a
   different endpoint?
4. Would someone unfamiliar with the project understand "the system is
   enforcing something, and it's cheaper" within the first ~15 seconds of
   watching, with no narration?

If the answer to any of these is no, that's a visual-language problem to
fix (colors, timing, edge behavior, graph layout/physics) — not a backend
problem.
