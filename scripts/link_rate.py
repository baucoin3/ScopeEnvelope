"""How well does a repo link PRs to tickets? Reads recent merged PRs through the GitHub API only.

usage: python scripts/link_rate.py owner/name [sample_size]
"""

import statistics
import sys

from scope_envelope.mine import linked_issues, list_merged_prs


def main() -> None:
    repo = sys.argv[1]
    sample = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    prs = list_merged_prs(repo, limit=sample)
    linked = [pr for pr in prs if linked_issues(pr)]
    bodies = [len(linked_issues(pr)[0].get("body") or "") for pr in linked]
    small = [pr for pr in linked if (pr.get("changedFiles") or 0) <= 25]
    print(
        f"{repo}: sampled={len(prs)} linked={len(linked)} ({100 * len(linked) // max(len(prs), 1)}%) "
        f"linked_and_<=25_files={len(small)} "
        f"median_ticket_chars={int(statistics.median(bodies)) if bodies else 0}"
    )
    for pr in linked[:3]:
        print(f"  PR #{pr['number']} <- ticket #{linked_issues(pr)[0]['number']}: {pr['title'][:90]}")


if __name__ == "__main__":
    main()
