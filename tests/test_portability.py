"""Config, git awareness and failure handling: what makes the gateway work in any repo."""

import subprocess

import pytest

from scope_envelope.config import CONFIG_FILE, Config, defaults, detect_test_command, load_config, write_config
from scope_envelope.gateway import Gateway
from scope_envelope.normalize import extract_subject
from scope_envelope.scope import Learned, Scope


class BrokenStore:
    """Atlas is down: every call raises."""

    def get_scope(self, *a, **k):
        raise ConnectionError("atlas down")

    def log(self, event):
        raise ConnectionError("atlas down")

    def save_footprint(self, fp):
        raise ConnectionError("atlas down")


class RecordingStore:
    def __init__(self, scope=None):
        self.scope, self.events, self.footprints = scope, [], []

    def get_scope(self, ticket_text, verb, subject, repo=None, before=None):
        if self.scope:
            self.scope.subject = subject
        return self.scope

    def log(self, event):
        self.events.append(event)

    def save_footprint(self, fp):
        self.footprints.append(fp)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "go.mod").write_text("module acme/widgets\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "widget.go").write_text("package pkg\n// TODO widget\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.go").write_text("// TODO generated\n")
    return tmp_path


@pytest.mark.parametrize(
    ("marker", "command"),
    [("go.mod", "go build ./..."), ("package.json", "npm test --silent"), ("pyproject.toml", "python -m pytest -q -x"),
     ("Cargo.toml", "cargo test -q")],
)
def test_detects_test_command_from_repo_type(tmp_path, marker, command):
    (tmp_path / marker).write_text("")
    assert detect_test_command(tmp_path) == command


def test_defaults_read_repo_from_git_remote(git_repo):
    config = defaults(git_repo)
    assert config.repo == "acme/widgets"
    assert config.test_command == "go build ./..."


def test_config_file_round_trip_keeps_regex_backslashes(git_repo):
    config = defaults(git_repo)
    config.subject_patterns = [r"\bacme_([a-z_]+)"]
    write_config(config)
    loaded = load_config(git_repo)
    assert loaded.subject_patterns == [r"\bacme_([a-z_]+)"]
    assert loaded.repo == "acme/widgets" and loaded.test_argv == ["go", "build", "./..."]


def test_empty_values_in_config_mean_none(tmp_path):
    (tmp_path / CONFIG_FILE).write_text("repo = ''\ntest_command = ''\n")
    config = load_config(tmp_path)
    assert config.repo is None and config.test_argv is None


def test_model_env_overrides_config(tmp_path, monkeypatch):
    (tmp_path / CONFIG_FILE).write_text("model = 'a/b'\n")
    monkeypatch.setenv("MODEL", "deepseek/deepseek-v4-pro")
    assert load_config(tmp_path).model == "deepseek/deepseek-v4-pro"


def test_custom_subject_pattern_wins():
    assert extract_subject("acme_gizmo_widget: add color", [r"\bacme_([a-z_]+)"]) == "gizmo widget"
    assert extract_subject("Fix OrderPlacedEvent", [r"\bacme_([a-z_]+)"]) == "order placed"


def test_listing_and_grep_respect_gitignore_and_see_new_files(git_repo):
    gw = Gateway("t-1", "Add widget color", git_repo, "lazy", store=None, verbose=False)
    assert "build/" not in gw.call("list_dir", {"path": "."})
    gw.call("write_file", {"path": "pkg/new.go", "content": "// TODO new\n"})
    hits = gw.call("grep", {"pattern": "TODO", "path": "."})
    assert "pkg/widget.go:2:" in hits and "pkg/new.go:1:" in hits
    assert "build/out.go" not in hits


def test_atlas_outage_never_breaks_tool_calls(git_repo):
    gw = Gateway("t-1", "Add widget color", git_repo, "envelope", store=BrokenStore(), verbose=False)
    assert gw.scope is None  # no scope, not a crash
    assert "widget.go" in gw.call("list_dir", {"path": "pkg"})
    summary = gw.finish({"passed": 1, "total": 1})
    assert summary["footprint_saved"] is False and summary["footprint_reason"].startswith("save failed")


def test_events_carry_model_and_scope_files_for_the_viewer(git_repo):
    scope = Scope(
        confidence="strong", subject=None, based_on=["acme/widgets#7"], call_budget=10, median_calls=8,
        files_changed=[Learned("pkg/{snake}.go", ["acme/widgets#7"])],
    )
    store = RecordingStore(scope)
    config = Config(root=git_repo, repo="acme/widgets", model="deepseek/deepseek-v4-pro")
    gw = Gateway("acme/widgets#9", "Add GizmoWidget support", git_repo, "envelope", config=config, store=store, verbose=False)
    gw.record_llm_step(1000, 50, 0.00098, "openrouter_billed")
    event = store.events[-1]
    assert event["model"] == "deepseek/deepseek-v4-pro"
    assert event["scope_files"] == ["pkg/gizmo_widget.go"]
    assert event["scope_based_on"] == ["acme/widgets#7"]
    assert event["totals"]["completion_tokens"] == 50
    summary = gw.finish({"passed": 1, "total": 1})
    assert summary["cost_source"] == "openrouter_billed"
    assert store.footprints[0]["model"] == "deepseek/deepseek-v4-pro"


def test_run_checks_without_test_command(tmp_path):
    gw = Gateway("t-1", "anything", tmp_path, "lazy", store=None, verbose=False)
    assert gw.run_checks() == {"passed": 0, "total": 0}
    assert "No test command" in gw.call("run_tests", {})
    assert gw.finish({"passed": 0, "total": 0})["footprint_reason"] == "no checks ran"


def test_run_checks_uses_configured_command(tmp_path):
    config = Config(root=tmp_path, test_command="python -c 'raise SystemExit(0)'")
    gw = Gateway("t-1", "anything", tmp_path, "lazy", config=config, store=None, verbose=False)
    assert gw.run_checks() == {"passed": 1, "total": 1}
