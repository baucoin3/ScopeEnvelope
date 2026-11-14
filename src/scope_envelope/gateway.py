"""The gateway: every agent tool call goes through here.

One Gateway per run. In "scoped" mode it fetches the learned scope, injects it
into the prompt and blocks writes outside it. In "raw" mode it only observes:
it runs every call and logs it, so the TUI can show both runs side by side.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .normalize import extract_subject, normalize
from .scope import Scope, render_prompt
from .store import Store

TOOL_KINDS = {
    "list_dir": "read",
    "read_file": "read",
    "grep": "read",
    "write_file": "write",
    "run_tests": "exec",
    "request_write": "ask",
}
VERBS = {
    "add": ("add", "implement", "create", "introduce", "build", "new"),
    "delete": ("remove", "delete", "drop", "deprecate"),
    "update": ("update", "change", "fix", "modify", "rename", "refactor", "bump", "migrate"),
}
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
PATH_EXTENSIONS = (".py", ".yaml", ".yml", ".json", ".md", ".toml", ".sql", ".txt", ".cfg", ".ini")
MAX_READ_CHARS = 60_000
MAX_GREP_HITS = 100

Approver = Callable[[str, str], tuple[bool, str]]
_FROM_ENV = object()


def extract_verb(ticket_text: str) -> str:
    for word in re.findall(r"[a-z]+", ticket_text.lower()):
        for verb, words in VERBS.items():
            if word in words:
                return verb
    return "other"


def extract_paths(ticket_text: str) -> list[str]:
    """Files or folders a person named in the ticket. Writes to these are allowed."""
    found = []
    for token in re.split(r"[\s`'\"(),;]+", ticket_text):
        token = token.strip(".:").removeprefix("./")
        if "/" not in token or token.startswith(("http:", "https:")):
            continue
        if token.endswith("/") or token.endswith(PATH_EXTENSIONS):
            found.append(token)
    return found


def env_approver() -> Approver:
    """SCOPE_APPROVAL=auto (default, approve and flag), deny, or prompt (ask in the terminal)."""
    policy = os.environ.get("SCOPE_APPROVAL", "auto")

    def approve(path: str, reason: str) -> tuple[bool, str]:
        if policy == "deny":
            return False, "denied by policy"
        if policy == "prompt":
            answer = input(f"\n[scope] Agent wants to write {path}: {reason}\nAllow? [y/N] ")
            return answer.strip().lower() == "y", "human decision"
        return True, "auto-approved"

    return approve


def _added_lines(old: str | None, new: str) -> list[str]:
    before = Counter(line.strip() for line in (old or "").splitlines())
    added = []
    for line in new.splitlines():
        key = line.strip()
        if not key:
            continue
        if before[key]:
            before[key] -= 1
        else:
            added.append(key)
    return added


class Gateway:
    def __init__(
        self,
        ticket_id: str,
        ticket_text: str,
        repo_root: str | Path,
        mode: str = "scoped",
        *,
        store: Store | None | object = _FROM_ENV,
        approver: Approver | None = None,
        test_command: list[str] | None = None,
        verbose: bool = True,
    ):
        if mode not in ("raw", "scoped"):
            raise ValueError("mode must be 'raw' or 'scoped'")
        self.ticket_id = ticket_id
        self.ticket_text = ticket_text
        self.mode = mode
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise FileNotFoundError(f"repo_root does not exist: {self.repo_root}")
        self.store: Store | None = Store.from_env() if store is _FROM_ENV else store  # type: ignore[assignment]
        self.approver = approver or env_approver()
        self.test_command = test_command or [sys.executable, "-m", "pytest", "-q", "-x"]
        self.verbose = verbose

        self.run_id = uuid.uuid4().hex[:12]
        self.subject = extract_subject(ticket_text)
        self.verb = extract_verb(ticket_text)
        self.ticket_paths = extract_paths(ticket_text)
        # Raw runs fetch the scope too, but never use it: it only measures noise for the comparison.
        self.scope: Scope | None = self.store.get_scope(ticket_text, self.verb, self.subject) if self.store else None

        self.step = 0
        self.calls = 0
        self.blocked = 0
        self.files_read: list[str] = []
        self.originals: dict[str, str | None] = {}
        self.approved: set[str] = set()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.token_steps: list[int] = []
        self.finished = False

        self._log("scope", decision="start", extra={"scope": self._scope_for_log()})

    # --- what the agent owner calls ---------------------------------------------

    @property
    def enforcing(self) -> bool:
        return self.mode == "scoped"

    def scope_prompt(self) -> str:
        return render_prompt(self.scope) if self.enforcing else ""

    def tools(self) -> list:
        from .tools import langchain_tools

        return langchain_tools(self)

    def record_llm_step(self, prompt_tokens: int, completion_tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cost_usd += cost_usd
        self.token_steps.append(prompt_tokens)
        self._log("llm", decision="llm", extra={
            "step_prompt_tokens": prompt_tokens,
            "step_completion_tokens": completion_tokens,
            "step_cost_usd": cost_usd,
        })

    def call(self, tool: str, args: dict) -> str:
        """Run one tool call through the gateway. Always returns text for the model."""
        kind = TOOL_KINDS.get(tool)
        if kind is None:
            self._log(tool, decision="error", reason="unknown tool")
            return f"Error: unknown tool '{tool}'. Available tools: {', '.join(TOOL_KINDS)}."
        try:
            if kind == "write":
                return self._write(args)
            if kind == "ask":
                return self._request_write(args)
            if kind == "exec":
                return self._counted(tool, None, "exec", self._run_tests())
            return self._read(tool, args)
        except (OSError, ValueError, re.error, subprocess.SubprocessError) as exc:
            self._log(tool, path=args.get("path"), decision="error", reason=str(exc))
            return f"Error: {exc}"

    def finish(self, checks: dict) -> dict:
        """Save the run as a footprint if it passed, and return the summary the TUI shows."""
        if self.finished:
            raise RuntimeError("finish() was already called for this run")
        self.finished = True
        total = int(checks.get("total", 0))
        passed = total > 0 and int(checks.get("passed", 0)) == total

        changed, added = self._diff()
        footprint = {
            "run_id": self.run_id,
            "ticket": self.ticket_id,
            "text": self.ticket_text,
            "verb": self.verb,
            "subject": self.subject,
            "condition": self.mode,
            "success": passed,
            "files_read": sorted({normalize(p, self.subject) for p in self.files_read}),
            "files_changed": sorted({normalize(p, self.subject) for p in changed}),
            "added_lines": sorted({normalize(line, self.subject) for line in added}),
            "calls": self.calls,
            "blocked": self.blocked,
            "approved_writes": sorted(self.approved),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "checks": {"passed": int(checks.get("passed", 0)), "total": total},
        }

        saved, why = False, "checks failed" if not passed else "no Atlas connection"
        if passed and self.store:
            self.store.save_footprint(footprint)
            saved, why = True, "passed"

        summary = {
            "ticket": self.ticket_id,
            "condition": self.mode,
            "passed": passed,
            "checks": footprint["checks"],
            "calls": self.calls,
            "blocked": self.blocked,
            "files_read": len(self.files_read),
            "files_changed": len(changed),
            "noise_files": self._noise_files(),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "scope_expansions": sorted(self.approved),
            "footprint_saved": saved,
            "footprint_reason": why,
            "lean": self._is_lean(),
            **self._line_report(footprint["added_lines"]),
            "history_for_next": (self.scope.matched if self.scope else 0) + (1 if saved else 0),
        }
        self._log("finish", decision="finish", extra={"summary": summary})
        return summary

    # --- tools -----------------------------------------------------------------

    def _resolve(self, path: str | None) -> tuple[Path, str]:
        candidate = (self.repo_root / (path or ".")).resolve()
        if candidate != self.repo_root and self.repo_root not in candidate.parents:
            raise ValueError(f"path is outside the repo: {path}")
        rel = candidate.relative_to(self.repo_root).as_posix()
        return candidate, "." if rel == "" else rel

    def _read(self, tool: str, args: dict) -> str:
        target, rel = self._resolve(args.get("path"))
        if tool == "list_dir":
            if not target.is_dir():
                raise ValueError(f"not a directory: {rel}")
            entries = sorted(
                (e.name + "/" if e.is_dir() else e.name) for e in target.iterdir() if e.name not in SKIP_DIRS
            )
            return self._counted(tool, rel, "read", "\n".join(entries) or "(empty)")
        if tool == "read_file":
            if not target.is_file():
                raise ValueError(f"file not found: {rel}")
            text = target.read_text(errors="replace")
            if rel not in self.files_read:
                self.files_read.append(rel)
            if len(text) > MAX_READ_CHARS:
                text = text[:MAX_READ_CHARS] + f"\n... (truncated, {len(text)} chars total)"
            return self._counted(tool, rel, "read", text)
        return self._counted(tool, rel, "read", self._grep(args.get("pattern", ""), target))

    def _grep(self, pattern: str, target: Path) -> str:
        if not pattern:
            raise ValueError("grep needs a pattern")
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        files = [target] if target.is_file() else sorted(
            p for p in target.rglob("*") if p.is_file() and not SKIP_DIRS.intersection(p.parts)
        )
        hits = []
        for file in files:
            try:
                for n, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{file.relative_to(self.repo_root).as_posix()}:{n}: {line.strip()}")
                        if len(hits) >= MAX_GREP_HITS:
                            return "\n".join(hits) + f"\n... (stopped at {MAX_GREP_HITS} matches)"
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"

    def _write(self, args: dict) -> str:
        target, rel = self._resolve(args.get("path"))
        allowed, reason = self._write_decision(rel)
        if not allowed:
            self.blocked += 1  # blocked calls don't count toward the budget
            self._log("write_file", path=rel, decision="blocked", reason=reason)
            return self._block_message(rel)
        if rel not in self.originals:
            self.originals[rel] = target.read_text(errors="replace") if target.is_file() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.get("content", ""))
        return self._counted("write_file", rel, "written", f"Wrote {rel}", reason=reason)

    def _request_write(self, args: dict) -> str:
        _, rel = self._resolve(args.get("path"))
        reason = args.get("reason", "")
        if self._write_decision(rel)[0]:
            return self._counted("request_write", rel, "approved", f"{rel} is already allowed.", reason="already allowed")
        ok, note = self.approver(rel, reason)
        if ok:
            self.approved.add(rel)
            return self._counted("request_write", rel, "approved", f"Approved: you may now write {rel}.", reason=note)
        return self._counted("request_write", rel, "denied", f"Denied: do not write {rel}.", reason=note)

    def _run_tests(self) -> str:
        proc = subprocess.run(
            self.test_command, cwd=self.repo_root, capture_output=True, text=True, timeout=300
        )
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-60:]
        return f"exit code {proc.returncode}\n" + "\n".join(tail)

    # --- decisions -------------------------------------------------------------

    def _write_decision(self, rel: str) -> tuple[bool, str]:
        if not self.enforcing:
            return True, "raw run (observe only)"
        if rel in self.approved:
            return True, "approved by a person"
        for named in self.ticket_paths:
            if rel == named or (named.endswith("/") and rel.startswith(named)):
                return True, "named in the ticket"
        if self.scope and self.scope.usable and self.scope.allows_write(rel):
            return True, "in learned scope"
        if self.scope and self.scope.usable:
            return False, "outside learned scope"
        return False, "no learned scope yet; needs approval"

    def _block_message(self, rel: str) -> str:
        if self.scope and self.scope.usable:
            allowed = self.scope.concrete(self.scope.files_changed) + sorted(self.approved)
            return (
                f"Blocked: {rel} is outside this ticket's scope. Allowed writes: {', '.join(allowed)}. "
                f"If this file is really required, call request_write(path, reason)."
            )
        return f"Blocked: there is no learned scope for this ticket yet. Call request_write(path='{rel}', reason=...) first."

    def _counted(self, tool: str, path: str | None, decision: str, result: str, reason: str = "") -> str:
        self.calls += 1
        self._log(tool, path=path, decision=decision, reason=reason)
        budget = self.scope.call_budget if (self.enforcing and self.scope and self.scope.usable) else None
        if budget and self.calls >= budget:
            result += f"\n[gateway] {self.calls} of about {budget} expected tool calls used. Finish the remaining work and stop."
        return result

    # --- reporting -------------------------------------------------------------

    def _diff(self) -> tuple[list[str], list[str]]:
        changed, added = [], []
        for rel, old in self.originals.items():
            path = self.repo_root / rel
            new = path.read_text(errors="replace") if path.is_file() else ""
            if new != (old or ""):
                changed.append(rel)
                added.extend(_added_lines(old, new))
        return changed, added

    def _noise_files(self) -> int | None:
        if not (self.scope and self.scope.usable):
            return None
        return sum(1 for p in self.files_read if not self.scope.covers(p))

    def _is_lean(self) -> bool | None:
        if not (self.scope and self.scope.median_calls):
            return None
        return self.calls <= self.scope.median_calls

    def _line_report(self, added_templates: list[str]) -> dict:
        if not (self.scope and self.scope.usable):
            return {"lines_confirmed": None, "lines_expected": 0, "new_lines": len(added_templates)}
        expected = {line.template for line in self.scope.lines}
        got = set(added_templates)
        return {
            "lines_confirmed": len(expected & got),
            "lines_expected": len(expected),
            "new_lines": len(got - expected),
        }

    def _scope_for_log(self) -> dict | None:
        if not (self.enforcing and self.scope):
            return None
        return self.scope.to_dict()

    def _log(self, tool: str, *, path: str | None = None, decision: str, reason: str = "", extra: dict | None = None) -> None:
        if tool != "scope" or decision != "start":
            self.step += 1
        event = {
            "run_id": self.run_id,
            "ticket": self.ticket_id,
            "condition": self.mode,
            "step": self.step,
            "tool": tool,
            "path": path,
            "decision": decision,
            "reason": reason,
            "scope_based_on": self.scope.based_on if (self.enforcing and self.scope) else [],
            "totals": {
                "calls": self.calls,
                "blocked": self.blocked,
                "files_read": len(self.files_read),
                "prompt_tokens": self.prompt_tokens,
                "cost_usd": round(self.cost_usd, 6),
            },
            "ts": datetime.now(UTC),
            **(extra or {}),
        }
        if self.store:
            self.store.log(event)
        if self.verbose:
            where = f" {path}" if path else ""
            note = f" ({reason})" if reason else ""
            print(f"[{self.mode} {self.ticket_id} #{self.step}] {tool}{where} -> {decision}{note}", file=sys.stderr)


__all__ = ["Gateway", "extract_paths", "extract_verb"]
