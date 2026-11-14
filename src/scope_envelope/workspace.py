"""A fresh, isolated copy of the repo for each run, so lazy and envelope start from the same state.

Git repos get a detached `git worktree` at the requested commit (fast even for very
large repos, and the commit can be a PR's parent when replaying an old ticket).
Other folders are copied. Workspaces live outside the target repo, under
SCOPE_WORKDIR (default: <system temp>/scope-envelope), and are kept after the run
so the change can be inspected; the next run of the same ticket and condition
replaces it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_SKIP = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache")


def workdir() -> Path:
    return Path(os.environ.get("SCOPE_WORKDIR") or Path(tempfile.gettempdir()) / "scope-envelope")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:80] or "run"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=600)


def prepare(repo_root: str | Path, ticket_id: str, condition: str, base: str = "HEAD") -> Path:
    """Create (or recreate) the workspace for one run and return its path."""
    root = Path(repo_root).resolve()
    target = workdir() / _slug(root.name) / f"{_slug(ticket_id)}-{condition}"
    target.parent.mkdir(parents=True, exist_ok=True)
    is_git = (root / ".git").exists()

    if target.exists():
        if is_git:
            _git(root, "worktree", "remove", "--force", str(target))
        shutil.rmtree(target, ignore_errors=True)
    if is_git:
        _git(root, "worktree", "prune")
        result = _git(root, "worktree", "add", "--detach", str(target), base)
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")
    else:
        shutil.copytree(root, target, ignore=_SKIP)
    return target
