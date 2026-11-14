"""Turn what the user typed (a ticket link, owner/repo#123, or plain text) into (ticket_id, ticket_text)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess

_GITHUB_ISSUE = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/(\d+)")
_SHORT_REF = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def parse_ref(ref: str) -> tuple[str, int] | None:
    ref = ref.strip()
    match = _GITHUB_ISSUE.match(ref) or _SHORT_REF.match(ref)
    return (match.group(1), int(match.group(2))) if match else None


def fetch_ticket(ref: str) -> tuple[str, str]:
    """GitHub issue links are fetched with `gh`; anything else is used as the ticket text itself."""
    parsed = parse_ref(ref)
    if parsed is None:
        ticket_id = "ticket-" + hashlib.sha1(ref.encode()).hexdigest()[:8]
        return ticket_id, ref.strip()
    repo, number = parsed
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "-R", repo, "--json", "title,body"],
        capture_output=True, text=True, check=True,
    ).stdout
    issue = json.loads(out)
    return f"{repo}#{number}", f"{issue['title']}\n\n{issue.get('body') or ''}".strip()
