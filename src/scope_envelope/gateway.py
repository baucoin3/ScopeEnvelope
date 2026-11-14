"""The gateway: every agent tool call goes through here.

One Gateway per run. In "envelope" mode it fetches the learned scope, injects it
into the prompt and blocks writes outside it. In "lazy" mode it only observes:
it runs every call and logs it, so the viewer can show both runs side by side.
"""

from __future__ import annotations

import difflib
import logging
import re
import subprocess
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import Config, load_config
from .normalize import extract_subject, normalize
from .scope import Scope, render_prompt
from .store import Store

log = logging.getLogger("scope_envelope")

MODES = ("lazy", "envelope")
TOOL_KINDS = {
    "list_dir": "read",
    "read_file": "read",
    "grep": "read",
    "write_file": "write",
    "run_tests": "exec",
    "request_write": "ask",
}
VERBS = {
    "add": ("add", "implement", "create", "introduce", "build", "new", "support"),
    "delete": ("remove", "delete", "drop", "deprecate"),
    "update": ("update", "change", "fix", "modify", "rename", "refactor", "bump", "migrate"),
}
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
PATH_EXTENSIONS = (".py", ".go", ".ts", ".js", ".java", ".yaml", ".yml", ".json", ".md", ".toml", ".sql", ".txt", ".tf")
MAX_READ_CHARS = 60_000
MAX_GREP_HITS = 100
MAX_DIFF_CHARS = 20_000
TEST_TIMEOUT_S = 600

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


def make_approver(policy: str) -> Approver:
    """auto (approve and flag in the summary), deny, or prompt (ask in the terminal)."""

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


def _change(rel: str, old: str | None, new: str) -> dict:
    """What a write did, for the browser: a unified diff (capped) and line counts."""
    diff = list(difflib.unified_diff(
        (old or "").splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{rel}", tofile=f"b/{rel}"
    ))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    text = "".join(diff)
    return {
        "change": "created" if old is None else "modified",
        "lines_added": added,
        "lines_removed": removed,
        "diff": text[:MAX_DIFF_CHARS] + ("\n... (diff truncated)" if len(text) > MAX_DIFF_CHARS else ""),
    }


class Gateway:
    def __init__(
        self,
        ticket_id: str,
        ticket_text: str,
        repo_root: str | Path,
        mode: str = "envelope",
        *,
        config: Config | None = None,
        store: Store | None | object = _FROM_ENV,
        approver: Approver | None = None,
        model: str | None = None,
        before: datetime | None = None,
        exclude_issue: int | None = None,
        run_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        verbose: bool = True,
    ):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise FileNotFoundError(f"repo_root does not exist: {self.repo_root}")
        self.config = config or load_config(self.repo_root)
        self.ticket_id = ticket_id
        self.ticket_text = ticket_text
        self.mode = mode
        self.model = model or self.config.model
        self.repo = self.config.repo
        self.before = before  # replaying an old ticket: only learn from history before it
        self.exclude_issue = exclude_issue  # the ticket's own issue number: never learn from its own fix
        self.store: Store | None = Store.from_env() if store is _FROM_ENV else store  # type: ignore[assignment]
        self.approver = approver or make_approver(self.config.approval)
        self.verbose = verbose
        self.on_event = on_event  # e.g. the interactive session printing friendly progress lines
        self.is_git = (self.repo_root / ".git").exists()

        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.subject = extract_subject(ticket_text, self.config.subject_patterns)
        self.verb = extract_verb(ticket_text)
        self.ticket_paths = extract_paths(ticket_text)
        # Lazy runs fetch the scope too, but never use it: it only measures noise for the comparison.
        self.scope: Scope | None = self._fetch_scope()

        self.step = 0
        self.calls = 0
        self.blocked = 0
        self.files_read: list[str] = []
        self.originals: dict[str, str | None] = {}
        self.approved: set[str] = set()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0
        self.cost_sources: set[str] = set()
        self.finished = False
        self._log_failed = False

        self._log("scope", decision="start", extra={"scope": self._scope_for_log()})

    # --- what the agent calls ------------------------------------------------------

    @property
    def enforcing(self) -> bool:
        return self.mode == "envelope"

    def scope_prompt(self) -> str:
        return render_prompt(self.scope) if self.enforcing else ""

    def tools(self) -> list:
        from .tools import langchain_tools

        return langchain_tools(self)

    def record_llm_step(
        self, prompt_tokens: int, completion_tokens: int = 0, cost_usd: float | None = None, cost_source: str = "unknown"
    ) -> None:
        """Report one model reply. cost_source: openrouter_billed | price_table | unknown."""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cost_usd += cost_usd or 0.0
        self.cost_sources.add(cost_source)
        self._log("llm", decision="llm", extra={
            "step_prompt_tokens": prompt_tokens,
            "step_completion_tokens": completion_tokens,
            "step_cost_usd": cost_usd,
            "cost_source": cost_source,
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
                return self._counted(tool, None, "exec", self._run_tests()[1])
            return self._read(tool, args)
        except (OSError, ValueError, re.error, subprocess.SubprocessError) as exc:
            self._log(tool, path=args.get("path"), decision="error", reason=str(exc))
            return f"Error: {exc}"

    def run_checks(self) -> dict:
        """Run the repo's test command once after the agent stops. Not counted as an agent call."""
        if not self.config.test_argv:
            self._log("checks", decision="skipped", reason="no test_command configured")
            return {"passed": 0, "total": 0}
        ok, output = self._run_tests()
        self._log("checks", decision="passed" if ok else "failed", reason=output.splitlines()[0] if output else "")
        return {"passed": int(ok), "total": 1}

    def finish(self, checks: dict) -> dict:
        """Save the run as a footprint if it passed, and return the summary the viewer shows."""
        if self.finished:
            raise RuntimeError("finish() was already called for this run")
        self.finished = True
        total = int(checks.get("total", 0))
        verified = total > 0
        passed = verified and int(checks.get("passed", 0)) == total
        # With no test command, save_unchecked lets a run still become history (marked unverified).
        keep = passed or (not verified and self.config.save_unchecked)

        changed, added = self._diff()
        footprint = {
            "run_id": self.run_id,
            "source": "agent_run",
            "repo": self.repo,
            "ticket": self.ticket_id,
            "issue": self.exclude_issue,
            "text": self.ticket_text,
            "merged_at": datetime.now(UTC),  # same field as mined PRs, so replay cutoffs apply to both
            "verb": self.verb,
            "subject": self.subject,
            "condition": self.mode,
            "model": self.model,
            "success": keep,
            "verified": verified,
            "files_read": sorted({normalize(p, self.subject) for p in self.files_read}),
            "files_changed": sorted({normalize(p, self.subject) for p in changed}),
            "added_lines": sorted({normalize(line, self.subject) for line in added}),
            "calls": self.calls,
            "blocked": self.blocked,
            "approved_writes": sorted(self.approved),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_source": self._cost_source(),
            "checks": {"passed": int(checks.get("passed", 0)), "total": total},
        }

        saved, why = False, ("checks failed" if verified else "no checks ran") if not keep else "no Atlas connection"
        if keep and self.store:
            try:
                self.store.save_footprint(footprint)
                saved, why = True, "passed" if passed else "saved without checks"
            except Exception as exc:  # telemetry must not break the run
                why = f"save failed: {type(exc).__name__}"
                log.warning("could not save footprint for %s: %s", self.ticket_id, exc)

        summary = {
            "ticket": self.ticket_id,
            "condition": self.mode,
            "model": self.model,
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
            "cost_source": footprint["cost_source"],
            "scope_expansions": sorted(self.approved),
            "footprint_saved": saved,
            "footprint_reason": why,
            "lean": self._is_lean(),
            **self._line_report(footprint["added_lines"]),
            "history_for_next": (self.scope.matched if self.scope else 0) + (1 if saved else 0),
        }
        self._log("finish", decision="finish", extra={"summary": summary})
        return summary

    # --- tools -----------------------------------------------------------------------

    def _resolve(self, path: str | None) -> tuple[Path, str]:
        candidate = (self.repo_root / (path or ".")).resolve()
        if candidate != self.repo_root and self.repo_root not in candidate.parents:
            raise ValueError(f"path is outside the repo: {path}")
        rel = candidate.relative_to(self.repo_root).as_posix()
        return candidate, "." if rel == "" else rel

    def _repo_files(self) -> list[str]:
        """Files the agent can see. In a git repo this respects .gitignore and includes new files."""
        if self.is_git:
            out = subprocess.run(
                ["git", "-C", str(self.repo_root), "ls-files", "--cached", "--others", "--exclude-standard"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode == 0:
                return [p for p in out.stdout.splitlines() if (self.repo_root / p).is_file()]
        return sorted(
            p.relative_to(self.repo_root).as_posix()
            for p in self.repo_root.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(self.repo_root).parts)
        )

    def _read(self, tool: str, args: dict) -> str:
        target, rel = self._resolve(args.get("path"))
        if tool == "list_dir":
            return self._counted(tool, rel, "read", self._list_dir(target, rel))
        if tool == "read_file":
            if not target.is_file():
                raise ValueError(f"file not found: {rel}")
            text = target.read_text(errors="replace")
            if rel not in self.files_read:
                self.files_read.append(rel)
            if len(text) > MAX_READ_CHARS:
                text = text[:MAX_READ_CHARS] + f"\n... (truncated, {len(text)} chars total)"
            return self._counted(tool, rel, "read", text)
        return self._counted(tool, rel, "read", self._grep(args.get("pattern", ""), rel))

    def _list_dir(self, target: Path, rel: str) -> str:
        if not target.is_dir():
            raise ValueError(f"not a directory: {rel}")
        prefix = "" if rel == "." else rel + "/"
        entries = set()
        for path in self._repo_files():
            if path.startswith(prefix):
                head, _, tail = path[len(prefix):].partition("/")
                entries.add(head + "/" if tail else head)
        return "\n".join(sorted(entries)) or "(empty)"

    def _grep(self, pattern: str, rel: str) -> str:
        if not pattern:
            raise ValueError("grep needs a pattern")
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        prefix = "" if rel == "." else rel.rstrip("/")
        files = [p for p in self._repo_files() if not prefix or p == prefix or p.startswith(prefix + "/")]
        hits = []
        for path in files:
            try:
                text = (self.repo_root / path).read_text(errors="strict")
            except (OSError, UnicodeDecodeError):  # skip binary and unreadable files
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{path}:{n}: {line.strip()[:300]}")
                    if len(hits) >= MAX_GREP_HITS:
                        return "\n".join(hits) + f"\n... (stopped at {MAX_GREP_HITS} matches)"
        return "\n".join(hits) or "(no matches)"

    def _write(self, args: dict) -> str:
        target, rel = self._resolve(args.get("path"))
        allowed, reason = self._write_decision(rel)
        if not allowed:
            self.blocked += 1  # blocked calls don't count toward the budget
            self._log("write_file", path=rel, decision="blocked", reason=reason)
            return self._block_message(rel)
        previous = target.read_text(errors="replace") if target.is_file() else None
        if rel not in self.originals:
            self.originals[rel] = previous
        content = args.get("content", "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return self._counted("write_file", rel, "written", f"Wrote {rel}", reason=reason, extra=_change(rel, previous, content))

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

    def _run_tests(self) -> tuple[bool, str]:
        argv = self.config.test_argv
        if not argv:
            return False, "No test command is configured for this repo (set test_command in .scope-envelope.toml)."
        if argv[0] == "python":  # use the interpreter running the gateway, not whatever is first on PATH
            argv = [sys.executable, *argv[1:]]
        proc = subprocess.run(argv, cwd=self.repo_root, capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-60:]
        return proc.returncode == 0, f"exit code {proc.returncode}\n" + "\n".join(tail)

    # --- decisions ---------------------------------------------------------------------

    def _fetch_scope(self) -> Scope | None:
        if not self.store:
            return None
        try:
            return self.store.get_scope(
                self.ticket_text, self.verb, self.subject,
                repo=self.repo, before=self.before, exclude_issue=self.exclude_issue,
            )
        except Exception as exc:  # no Atlas means no scope, not a crashed run
            log.warning("could not fetch scope for %s: %s", self.ticket_id, exc)
            return None

    def _write_decision(self, rel: str) -> tuple[bool, str]:
        if not self.enforcing:
            return True, "lazy run (observe only)"
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

    def _counted(
        self, tool: str, path: str | None, decision: str, result: str, reason: str = "", extra: dict | None = None
    ) -> str:
        self.calls += 1
        self._log(tool, path=path, decision=decision, reason=reason, extra=extra)
        budget = self.scope.call_budget if (self.enforcing and self.scope and self.scope.usable) else None
        if budget and self.calls >= budget:
            result += f"\n[gateway] {self.calls} of about {budget} expected tool calls used. Finish the remaining work and stop."
        return result

    # --- reporting ---------------------------------------------------------------------

    def _diff(self) -> tuple[list[str], list[str]]:
        changed, added = [], []
        for rel, old in self.originals.items():
            path = self.repo_root / rel
            new = path.read_text(errors="replace") if path.is_file() else ""
            if new != (old or ""):
                changed.append(rel)
                added.extend(_added_lines(old, new))
        return changed, added

    def _cost_source(self) -> str:
        sources = self.cost_sources - {"unknown"} or self.cost_sources
        return sources.pop() if len(sources) == 1 else ("mixed" if sources else "none")

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

    def _scope_files(self) -> list[str]:
        """Concrete files in the learned scope, for the viewer's dashed 'learned scope' tiles."""
        if not (self.enforcing and self.scope and self.scope.usable):
            return []
        return sorted(set(self.scope.concrete(self.scope.files_changed) + self.scope.concrete(self.scope.files_read)))

    def _log(self, tool: str, *, path: str | None = None, decision: str, reason: str = "", extra: dict | None = None) -> None:
        if tool != "scope":
            self.step += 1
        event = {
            "run_id": self.run_id,
            "ticket": self.ticket_id,
            "repo": self.repo,
            "condition": self.mode,
            "model": self.model,
            "step": self.step,
            "tool": tool,
            "path": path,
            "decision": decision,
            "reason": reason,
            "scope_based_on": self.scope.based_on if (self.enforcing and self.scope) else [],
            "scope_files": self._scope_files(),
            "totals": {
                "calls": self.calls,
                "blocked": self.blocked,
                "files_read": len(self.files_read),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cost_usd": round(self.cost_usd, 6),
            },
            "ts": datetime.now(UTC),
            **(extra or {}),
        }
        if self.store:
            try:
                self.store.log(event)
            except Exception as exc:  # telemetry must never break the agent's work
                if not self._log_failed:
                    log.warning("could not write to runs (continuing without telemetry): %s", exc)
                    self._log_failed = True
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as exc:  # a display problem must not break the run
                log.warning("on_event callback failed: %s", exc)
        if self.verbose:
            where = f" {path}" if path else ""
            note = f" ({reason})" if reason else ""
            print(f"[{self.mode} {self.ticket_id} #{self.step}] {tool}{where} -> {decision}{note}", file=sys.stderr)


__all__ = ["Gateway", "MODES", "extract_paths", "extract_verb"]
