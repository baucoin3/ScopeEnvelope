"""Per-repo settings, read from .scope-envelope.toml in the target repo.

Nothing about a particular repo is hard-coded in the gateway: which GitHub repo
its history comes from, how to run its tests and how its names look all live
here. `scope-envelope init` writes the file with detected defaults.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = ".scope-envelope.toml"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_MAX_CALLS = 40
DEFAULT_MAX_PROMPT_TOKENS = 1_500_000

# Marker file -> test command. First match wins.
_TEST_COMMANDS = [
    ("go.mod", "go build ./..."),
    ("Cargo.toml", "cargo test -q"),
    ("package.json", "npm test --silent"),
    ("pyproject.toml", "python -m pytest -q -x"),
    ("setup.py", "python -m pytest -q -x"),
    ("pytest.ini", "python -m pytest -q -x"),
    ("gradlew", "./gradlew test -q"),
    ("pom.xml", "mvn -q test"),
]


@dataclass
class Config:
    root: Path
    repo: str | None = None  # owner/name whose history to learn from
    test_command: str | None = None  # how to check the agent's change; None = no check
    subject_patterns: list[str] = field(default_factory=list)  # extra regexes for ticket subjects
    approval: str = "auto"  # request_write policy: auto | deny | prompt
    model: str = DEFAULT_MODEL
    max_calls: int = DEFAULT_MAX_CALLS
    save_unchecked: bool = False  # with no test_command, still save passing-looking runs as history
    auto_learn: bool = True  # envelope runs learn the repo first when Atlas has no recent history
    learn_limit: int = 100  # how many ticket-linked PRs auto-learn ingests
    learn_max_age_days: int = 7  # history older than this is refreshed
    max_prompt_tokens: int = DEFAULT_MAX_PROMPT_TOKENS  # hard stop: total prompt tokens per run

    @property
    def test_argv(self) -> list[str] | None:
        return shlex.split(self.test_command) if self.test_command else None


KEYS = (
    "repo", "test_command", "subject_patterns", "approval", "model", "max_calls",
    "save_unchecked", "auto_learn", "learn_limit", "learn_max_age_days", "max_prompt_tokens",
)


def repo_from_remote(root: Path) -> str | None:
    """owner/name from the git origin remote, if it points at GitHub."""
    import re

    try:
        url = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def detect_test_command(root: Path) -> str | None:
    for marker, command in _TEST_COMMANDS:
        if (root / marker).exists():
            return command
    return None


def defaults(root: Path) -> Config:
    root = Path(root).resolve()
    return Config(root=root, repo=repo_from_remote(root), test_command=detect_test_command(root))


def load_config(root: str | Path) -> Config:
    """Settings for the repo at `root`: the config file if present, otherwise detected defaults.

    MODEL and SCOPE_APPROVAL in the environment override the file.
    """
    config = defaults(Path(root))
    # A detected test command is only a suggestion that `init` writes into the file. Without a
    # config file nothing runs after the agent, so a fresh repo works with zero setup and no build wait.
    config.test_command = None
    path = config.root / CONFIG_FILE
    if path.is_file():
        data = tomllib.loads(path.read_text())
        for key in KEYS:
            if key in data:
                value = data[key]
                setattr(config, key, (value or None) if key in ("repo", "test_command") else value)
    config.model = os.environ.get("MODEL", config.model)
    config.approval = os.environ.get("SCOPE_APPROVAL", config.approval)
    return config


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "") + "'"  # literal string: backslashes in regexes stay as typed


def write_config(config: Config) -> Path:
    lines = [
        "# ScopeEnvelope settings for this repo. Edit freely; every key is optional.",
        f"repo = {_toml_value(config.repo or '')}              # GitHub owner/name to learn history from",
        f"test_command = {_toml_value(config.test_command or '')}  # how to check a change (empty = no check)",
        f"subject_patterns = {_toml_value(config.subject_patterns)}  # regexes whose first group names the ticket's subject",
        f"approval = {_toml_value(config.approval)}           # request_write: auto | deny | prompt",
        f"model = {_toml_value(config.model)}",
        f"max_calls = {config.max_calls}                   # tool-call limit per run",
        f"save_unchecked = {_toml_value(config.save_unchecked)}         # no test_command: still save runs as history",
        f"auto_learn = {_toml_value(config.auto_learn)}              # envelope runs learn the repo first if Atlas has no recent history",
        f"learn_limit = {config.learn_limit}                # ticket-linked PRs to ingest when auto-learning",
        f"learn_max_age_days = {config.learn_max_age_days}           # refresh history older than this",
    ]
    path = config.root / CONFIG_FILE
    path.write_text("\n".join(lines) + "\n")
    return path


def env_files() -> list[Path]:
    """Where settings (.env) are looked for, in order. Earlier files win; real env vars beat all.

    1. a .env in the current folder or any folder above it (per-project override)
    2. ~/.config/scope-envelope/.env (per-user, works from any repo)
    3. the ScopeEnvelope checkout's own .env (when installed with --editable)
    """
    from dotenv import find_dotenv

    candidates = [
        find_dotenv(usecwd=True),
        Path.home() / ".config" / "scope-envelope" / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    seen, found = set(), []
    for c in candidates:
        path = Path(c) if c else None
        if path and path.is_file() and path.resolve() not in seen:
            seen.add(path.resolve())
            found.append(path)
    return found


def load_env() -> None:
    from dotenv import load_dotenv

    for path in env_files():
        load_dotenv(path, override=False)
