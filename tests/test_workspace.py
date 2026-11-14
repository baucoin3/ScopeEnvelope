import subprocess

import pytest

from scope_envelope import workspace


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_WORKDIR", str(tmp_path / "work"))
    root = tmp_path / "widgets"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "app.py").write_text("v = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "one")
    (root / "app.py").write_text("v = 2\n")
    git(root, "commit", "-qam", "two")
    return root


def test_git_workspace_is_a_clean_worktree_outside_the_repo(repo):
    (repo / "app.py").write_text("v = 99  # uncommitted\n")
    ws = workspace.prepare(repo, "acme/widgets#7", "envelope")
    assert repo not in ws.parents
    assert (ws / "app.py").read_text() == "v = 2\n"  # committed state, not the dirty checkout


def test_lazy_and_envelope_get_separate_workspaces(repo):
    lazy = workspace.prepare(repo, "acme/widgets#7", "lazy")
    env = workspace.prepare(repo, "acme/widgets#7", "envelope")
    (lazy / "app.py").write_text("changed by lazy\n")
    assert (env / "app.py").read_text() == "v = 2\n"


def test_rerun_replaces_the_workspace(repo):
    ws = workspace.prepare(repo, "acme/widgets#7", "envelope")
    (ws / "app.py").write_text("agent edit\n")
    again = workspace.prepare(repo, "acme/widgets#7", "envelope")
    assert again == ws and (again / "app.py").read_text() == "v = 2\n"


def test_base_commit_for_replays(repo):
    ws = workspace.prepare(repo, "acme/widgets#7", "lazy", base="HEAD~1")
    assert (ws / "app.py").read_text() == "v = 1\n"


def test_non_git_folder_is_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOPE_WORKDIR", str(tmp_path / "work"))
    src = tmp_path / "plain"
    (src / "node_modules").mkdir(parents=True)
    (src / "main.py").write_text("x = 1\n")
    ws = workspace.prepare(src, "ticket-1", "lazy")
    assert (ws / "main.py").exists() and not (ws / "node_modules").exists()


def test_old_mode_names_still_work_in_agent_run_shim():
    from agent.run import _translate

    assert _translate(["PIPE-1", "--mode", "scoped"]) == ["run", "PIPE-1", "--condition", "envelope"]
    assert _translate(["PIPE-1", "--mode", "raw"]) == ["run", "PIPE-1", "--condition", "lazy"]
