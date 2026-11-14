"""The learned scope (envelope) for one ticket, and how it is built from Atlas output."""

from __future__ import annotations

import os
import statistics
from dataclasses import asdict, dataclass, field

from .normalize import denormalize, path_matcher

# Vector Search scores below WEAK_SCORE don't count as a match at all.
# Thresholds are starting points; tune them once real tickets are seeded.
STRONG_SCORE = float(os.environ.get("SCOPE_STRONG_SCORE", "0.80"))
WEAK_SCORE = float(os.environ.get("SCOPE_WEAK_SCORE", "0.65"))
STRONG_MIN_RUNS = 2
STRONG_HEADROOM = 2
WEAK_HEADROOM = 6


@dataclass
class Learned:
    template: str  # normalized, e.g. pipeline/consumers/{snake}_consumer.py
    sources: list[str]  # tickets it was learned from (provenance)


@dataclass
class Scope:
    confidence: str  # "strong" | "weak" | "none"
    subject: str | None
    files_read: list[Learned] = field(default_factory=list)
    files_changed: list[Learned] = field(default_factory=list)
    lines: list[Learned] = field(default_factory=list)
    based_on: list[str] = field(default_factory=list)
    median_calls: float | None = None
    call_budget: int | None = None
    matched: int = 0
    top_score: float = 0.0

    @property
    def usable(self) -> bool:
        return self.confidence != "none"

    def concrete(self, items: list[Learned]) -> list[str]:
        return [denormalize(item.template, self.subject) for item in items]

    def allows_write(self, path: str) -> bool:
        return any(path_matcher(f.template, self.subject).match(path) for f in self.files_changed)

    def covers(self, path: str) -> bool:
        """True if the path is one the scope expects to be read or changed."""
        templates = self.files_read + self.files_changed
        return any(path_matcher(f.template, self.subject).match(path) for f in templates)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["files_read_concrete"] = self.concrete(self.files_read)
        data["files_changed_concrete"] = self.concrete(self.files_changed)
        data["lines_concrete"] = self.concrete(self.lines)
        return data


def _learned(rows: list[dict]) -> list[Learned]:
    return [Learned(template=row["_id"], sources=sorted(row["from"])) for row in rows]


def scope_from_facets(doc: dict | None, subject: str | None) -> Scope:
    """Build a Scope from the $facet output of the Atlas scope pipeline."""
    meta = (doc or {}).get("meta") or []
    if not meta:
        return Scope(confidence="none", subject=subject)
    meta = meta[0]
    top_score = float(meta.get("top_score") or 0.0)
    lean_runs = int(meta.get("n") or 0)
    if lean_runs == 0 or top_score < WEAK_SCORE:
        return Scope(confidence="none", subject=subject, matched=int(meta.get("matched") or 0), top_score=top_score)

    strong = top_score >= STRONG_SCORE and lean_runs >= STRONG_MIN_RUNS
    calls = [c for c in (meta.get("calls") or []) if c is not None]  # mined PRs have no call count
    median_calls = statistics.median(calls) if calls else None
    budget = None
    if median_calls is not None:
        budget = int(round(median_calls)) + (STRONG_HEADROOM if strong else WEAK_HEADROOM)
    return Scope(
        confidence="strong" if strong else "weak",
        subject=subject,
        files_read=_learned(doc.get("files_read", [])),
        files_changed=_learned(doc.get("files_changed", [])),
        lines=_learned(doc.get("lines", [])),
        based_on=list(meta.get("based_on") or []),
        median_calls=median_calls,
        call_budget=budget,
        matched=int(meta.get("matched") or 0),
        top_score=top_score,
    )


def render_prompt(scope: Scope | None) -> str:
    """The text injected into the agent's prompt in scoped mode."""
    if scope is None or not scope.usable:
        return (
            "\n\n## Scope\n"
            "There is no learned history for this kind of ticket yet. Before writing any file, "
            "call request_write(path, reason) and wait for approval.\n"
        )
    lines = [
        "",
        "",
        f"## Scope for this ticket (learned from {', '.join(scope.based_on)})",
        "Similar past tickets were completed by changing these files:",
        *[f"- {p}" for p in scope.concrete(scope.files_changed)],
    ]
    reads = [p for p in scope.concrete(scope.files_read) if p not in scope.concrete(scope.files_changed)]
    if reads:
        lines += ["They also read these files for context:", *[f"- {p}" for p in reads]]
    if scope.lines:
        lines += ["Conventions those tickets followed (lines they added):", *[f"- {l}" for l in scope.concrete(scope.lines)]]
    if scope.call_budget:
        lines.append(f"Expect to finish in about {scope.call_budget} tool calls.")
    if scope.confidence == "weak":
        lines.append("This match is low confidence: check these conventions against the repo before relying on them.")
    lines.append(
        "Writes outside the files above are blocked. If you really need another file, "
        "call request_write(path, reason)."
    )
    return "\n".join(lines) + "\n"
