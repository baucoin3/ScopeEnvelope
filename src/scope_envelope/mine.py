"""Learn a repo's history: turn its merged pull requests into footprints.

Each merged PR is a past ticket that was done successfully (it passed review and CI).
Its title plus the issue it closed become the ticket text; the files it changed and
the lines it added become the footprint, normalized so they apply to new tickets.
Uses the `gh` CLI, so it reads GitHub as whichever account `gh` is logged in as.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .gateway import extract_verb
from .normalize import extract_subject, normalize

MAX_TEXT_CHARS = 4000
MAX_LINES_PER_PR = 300
_TRIVIAL_LINE = re.compile(r"^[\s{}()\[\],;:]*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CHECKBOX = re.compile(r"^\s*[-*] \[[ xX]\].*$", re.MULTILINE)

_SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title body url mergedAt changedFiles additions
        closingIssuesReferences(first: 3) { nodes { number title body } }
      }
    }
  }
}
"""


@dataclass
class LearnReport:
    repo: str
    found: int
    skipped_large: int
    saved: int


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def list_merged_prs(repo: str, search: str = "", limit: int = 200) -> list[dict]:
    """Newest merged PRs matching an optional GitHub search (e.g. 'label:enhancement')."""
    query = f"repo:{repo} is:pr is:merged sort:created-desc {search}".strip()
    prs: list[dict] = []
    after = None
    while len(prs) < limit:
        args = ["api", "graphql", "-f", f"query={_SEARCH}", "-f", f"q={query}"]
        if after:
            args += ["-f", f"after={after}"]
        page = json.loads(_gh(args))["data"]["search"]
        prs += [node for node in page["nodes"] if node and node.get("number")]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return prs[:limit]


def pr_files(repo: str, number: int) -> list[dict]:
    out = _gh(["api", f"repos/{repo}/pulls/{number}/files", "--paginate", "--jq", ".[] | {filename, status, patch}"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _clean(text: str | None) -> str:
    text = _HTML_COMMENT.sub("", text or "")
    return _CHECKBOX.sub("", text).strip()


def ticket_text(pr: dict) -> str:
    """The PR title plus the issue(s) it closed; falls back to the PR description."""
    parts = [pr["title"]]
    issues = linked_issues(pr)
    for issue in issues:
        parts += [issue.get("title", ""), _clean(issue.get("body"))]
    if not issues:
        parts.append(_clean(pr.get("body")))
    return "\n\n".join(p for p in parts if p)[:MAX_TEXT_CHARS]


def added_lines(patch: str | None) -> list[str]:
    lines = []
    for line in (patch or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if len(text) >= 4 and not _TRIVIAL_LINE.match(text):
                lines.append(text)
    return lines


def linked_issues(pr: dict) -> list[dict]:
    return (pr.get("closingIssuesReferences") or {}).get("nodes") or []


def footprint_from_pr(repo: str, pr: dict, files: list[dict]) -> dict:
    text = ticket_text(pr)
    issues = linked_issues(pr)
    subject = extract_subject(pr["title"]) or extract_subject(text)
    seen: dict[str, None] = {}
    for f in files:
        for line in added_lines(f.get("patch")):
            seen.setdefault(normalize(line, subject))
            if len(seen) >= MAX_LINES_PER_PR:
                break
    return {
        "source": "github_pr",
        "repo": repo,
        "ticket": f"{repo}#{pr['number']}",
        "url": pr.get("url"),
        # The ticket this PR delivered (provenance: ticket #issue -> PR #number).
        "issue": issues[0]["number"] if issues else None,
        "issue_url": f"https://github.com/{repo}/issues/{issues[0]['number']}" if issues else None,
        "text": text,
        "verb": extract_verb(pr["title"]),
        "subject": subject,
        "condition": "human",
        "success": True,  # merged: it passed review and CI
        "merged_at": datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00")),
        "files_read": [],  # a human PR doesn't record what was read; agent runs fill this in later
        "files_changed": sorted({normalize(f["filename"], subject) for f in files}),
        "added_lines": list(seen),
        "calls": None,
        "additions": pr.get("additions"),
    }


def learn(
    repo: str,
    store,
    search: str = "",
    limit: int = 200,
    max_files: int = 25,
    require_issue: bool = True,
    before: datetime | None = None,
    exclude_issue: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    workers: int = 8,
) -> LearnReport:
    """Mine merged PRs into the footprints collection. Safe to re-run: PRs are upserted.

    With require_issue (the default), only PRs that close a ticket are learned, because
    the ticket is what a future ticket will be matched against. `before` and
    `exclude_issue` keep the ticket being worked on from learning its own fix.
    """
    if before:
        search = f"{search} merged:<{before.date().isoformat()}".strip()
    if require_issue and "linked:" not in search:
        search = f"{search} linked:issue".strip()
    prs = list_merged_prs(repo, search, limit)
    if require_issue:
        prs = [pr for pr in prs if linked_issues(pr)]
    if exclude_issue is not None:
        prs = [pr for pr in prs if exclude_issue not in {i["number"] for i in linked_issues(pr)}]
    small = [pr for pr in prs if (pr.get("changedFiles") or 0) <= max_files]

    files_by_pr: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(pr_files, repo, pr["number"]): pr["number"] for pr in small}
        for done, future in enumerate(as_completed(futures), 1):
            files_by_pr[futures[future]] = future.result()
            if on_progress:
                on_progress(done, len(small))
    footprints = [footprint_from_pr(repo, pr, files_by_pr[pr["number"]]) for pr in small]
    saved = store.upsert_footprints(footprints) if footprints else 0
    store.mark_learned(repo, saved)
    return LearnReport(repo=repo, found=len(prs), skipped_large=len(prs) - len(small), saved=saved)


def needs_learning(status: dict | None, max_age_days: int) -> bool:
    """True when the repo has no history in Atlas, or it was learned too long ago."""
    if not status or not status.get("footprints"):
        return True
    learned_at = status.get("last_learned_at")
    if learned_at is None:
        return True
    if learned_at.tzinfo is None:
        learned_at = learned_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - learned_at > timedelta(days=max_age_days)
