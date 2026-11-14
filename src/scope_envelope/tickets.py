"""Turn what the user typed (a ticket link, owner/repo#123, or plain text) into a Ticket."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

_GITHUB_ISSUE = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/(\d+)")
_SHORT_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


@dataclass
class Ticket:
    id: str  # owner/repo#123, or ticket-<hash> for plain text
    text: str  # title + body: what the agent and the scope search see
    repo: str | None = None
    number: int | None = None
    created_at: datetime | None = None  # used so history never includes this ticket's own fix


def parse_ref(ref: str) -> tuple[str, int] | None:
    ref = ref.strip()
    match = _GITHUB_ISSUE.match(ref) or _SHORT_REF.match(ref)
    return (match.group(1), int(match.group(2))) if match else None


def fetch_ticket(ref: str) -> Ticket:
    """GitHub issue links are fetched with `gh`; anything else is used as the ticket text itself."""
    parsed = parse_ref(ref)
    if parsed is None:
        return Ticket(id="ticket-" + hashlib.sha1(ref.encode()).hexdigest()[:8], text=ref.strip())
    repo, number = parsed
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "-R", repo, "--json", "title,body,createdAt"],
        capture_output=True, text=True, check=True,
    ).stdout
    issue = json.loads(out)
    return Ticket(
        id=f"{repo}#{number}",
        text=f"{issue['title']}\n\n{issue.get('body') or ''}".strip(),
        repo=repo,
        number=number,
        created_at=datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00")),
    )
