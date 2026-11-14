"""Shared wire-event contract (visualization-plan.md §1) and the
normalization step that turns raw run/footprint docs -- the same
`(event_type, doc)` shape MongoSource and mock_source both yield -- into
`WireEvent`s for the browser.

Reuses viewer.core.state's scope-tracking/tool-call-counting/classification
logic almost as-is (apply_run_event / apply_footprint_event). The only
thing this module adds on top is the running token/cost totals the wire
contract needs that state.py never had to compute, since the terminal
viewer only ever displayed one final footprint total, not a per-step
running sum.
"""

from dataclasses import asdict, dataclass

from ..viewer.core import state as state_mod


@dataclass
class WireEvent:
    condition: str  # "lazy" | "envelope"
    type: str  # "start" | "call" | "blocked" | "done"
    step: int
    tool: str | None
    path: str | None
    reason: str | None
    in_scope: bool
    prompt_tokens_cum: int | None
    cost_usd_cum: float | None
    tool_calls_cum: int
    run_id: str | None = None
    diff: str | None = None  # write_file only: what changed

    def to_dict(self) -> dict:
        return asdict(self)


class ConditionNormalizer:
    """One of these per condition (lazy/envelope). bridge.py (and, for
    now, mock_source.py's standalone __main__ block) feeds it raw
    (event_type, doc) tuples and gets WireEvents back.
    """

    def __init__(self, ticket: str, condition: str):
        self.condition = condition
        self._state = state_mod.new_state(ticket, condition)
        self._tokens_cum = 0
        self._cost_cum = 0.0

    def normalize(self, event_type: str, doc: dict) -> WireEvent:
        if event_type == "run":
            return self._normalize_run(doc)
        if event_type == "footprint":
            return self._normalize_footprint(doc)
        raise ValueError(f"unknown event_type: {event_type!r}")

    def _normalize_run(self, doc: dict) -> WireEvent:
        state_mod.apply_run_event(self._state, doc)
        totals = doc.get("totals")
        if totals:  # live gateway events carry running totals
            self._tokens_cum = totals.get("prompt_tokens") or 0
            self._cost_cum = totals.get("cost_usd") or 0.0
        else:  # mock events carry per-call numbers
            self._tokens_cum += doc.get("prompt_tokens") or 0
            self._cost_cum += doc.get("cost_usd") or 0.0

        tool = doc.get("tool")
        decision = doc.get("decision", "allowed")
        if tool == "finish":
            event_type = "done"
        elif tool == "scope" and decision == "start":
            event_type = "start"
        else:
            event_type = "blocked" if decision == "blocked" else "call"
        # Only file reads and writes become nodes; folder listings and searches stay counters-only.
        path = doc.get("path") if tool in ("read_file", "write_file") else None

        return WireEvent(
            condition=self.condition,
            type=event_type,
            step=doc.get("step", self._state.tool_calls),
            tool=tool,
            path=path,
            reason=doc.get("reason"),
            in_scope=bool(path) and path in self._state.scope,
            prompt_tokens_cum=self._tokens_cum or None,
            cost_usd_cum=self._cost_cum or None,
            tool_calls_cum=self._state.tool_calls,
            run_id=doc.get("run_id"),
            diff=doc.get("diff"),
        )

    def _normalize_footprint(self, doc: dict) -> WireEvent:
        state_mod.apply_footprint_event(self._state, doc)
        return WireEvent(
            condition=self.condition,
            type="done",
            step=self._state.tool_calls,
            tool=None,
            path=None,
            reason=None,
            in_scope=False,
            prompt_tokens_cum=self._state.prompt_tokens,
            cost_usd_cum=self._state.cost_usd,
            tool_calls_cum=doc.get("calls", self._state.tool_calls),
        )
