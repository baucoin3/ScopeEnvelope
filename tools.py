"""Plain functions that operate on a repo. No LangChain imports here.

Every function returns a plain dict/string result, never raises -- errors
are returned as text so the model can see and react to them.
"""

import re
import subprocess
import sys
from pathlib import Path


def _safe_path(repo_root: str, rel_path: str) -> Path:
    """Resolve rel_path under repo_root, raising ValueError if it escapes."""
    root = Path(repo_root).resolve()
    candidate = (root / rel_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError(f"path '{rel_path}' escapes repo root")
    return candidate


def list_dir(repo_root: str, path: str = ".") -> dict:
    try:
        target = _safe_path(repo_root, path)
        if not target.exists():
            return {"error": f"path not found: {path}"}
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return {"entries": entries}
    except Exception as e:
        return {"error": str(e)}


def read_file(repo_root: str, path: str) -> dict:
    try:
        target = _safe_path(repo_root, path)
        if not target.is_file():
            return {"error": f"file not found: {path}"}
        return {"content": target.read_text()}
    except Exception as e:
        return {"error": str(e)}


def grep(repo_root: str, pattern: str, path: str = ".") -> dict:
    try:
        target = _safe_path(repo_root, path)
        regex = re.compile(pattern)
        matches = []
        files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
        for f in files:
            try:
                for i, line in enumerate(f.read_text().splitlines(), start=1):
                    if regex.search(line):
                        matches.append(f"{f.relative_to(Path(repo_root).resolve())}:{i}:{line}")
            except (UnicodeDecodeError, OSError):
                continue
        return {"matches": matches}
    except Exception as e:
        return {"error": str(e)}


def write_file(repo_root: str, path: str, content: str) -> dict:
    try:
        target = _safe_path(repo_root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return {"written": path}
    except Exception as e:
        return {"error": str(e)}


def run_tests(repo_root: str) -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except Exception as e:
        return {"error": str(e)}
