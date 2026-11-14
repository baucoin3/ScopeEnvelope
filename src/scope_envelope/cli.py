"""scope-envelope command line.

    scope-envelope init                      # in the target repo: write .scope-envelope.toml
    scope-envelope setup                     # create the Atlas collections and Vector Search index
    scope-envelope learn                     # mine the repo's merged, ticket-linked PRs into Atlas
    scope-envelope scope "<ticket>"          # show the scope learned for a ticket
    scope-envelope run <ticket>              # run the agent on a ticket (link, owner/repo#123 or text)
    scope-envelope view <ticket-id>          # live map of a run (start before `run`)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import CONFIG_FILE, defaults, load_config, write_config
from .gateway import MODES, extract_verb
from .normalize import extract_subject
from .scope import render_prompt
from .store import Store


def _store() -> Store:
    store = Store.from_env()
    if store is None:
        sys.exit("MONGODB_URI is not set. Add it to .env (see .env.example).")
    return store


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).replace(tzinfo=UTC) if value else None


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.repo_root).resolve()
    path = root / CONFIG_FILE
    if path.exists() and not args.force:
        sys.exit(f"{path} already exists (use --force to overwrite).")
    config = defaults(root)
    written = write_config(config)
    print(f"Wrote {written}")
    print(f"  repo:         {config.repo or '(not a GitHub remote; set it by hand)'}")
    print(f"  test_command: {config.test_command or '(none detected; set it by hand)'}")


def cmd_setup(_: argparse.Namespace) -> None:
    store = _store()
    print(f"Vector Search index: {store.ensure_indexes()}")
    print("Index is ready." if store.wait_until_queryable(5) else "Index is building; it can be queried in a minute or two.")


def cmd_learn(args: argparse.Namespace) -> None:
    from .mine import learn

    repo = args.repo or load_config(args.repo_root).repo
    if not repo:
        sys.exit("No repo given and none in .scope-envelope.toml. Pass owner/name or run `scope-envelope init`.")
    store = _store()
    store.ensure_indexes()
    print(f"Learning from merged PRs in {repo} ...")
    report = learn(
        repo, store, search=args.search, limit=args.limit, max_files=args.max_files, require_issue=not args.include_unlinked
    )
    linked = "merged" if args.include_unlinked else "ticket-linked"
    print(
        f"Found {report.found} {linked} PRs, skipped {report.skipped_large} with more than {args.max_files} files, "
        f"saved {report.saved} footprints."
    )


def cmd_scope(args: argparse.Namespace) -> None:
    config = load_config(args.repo_root)
    repo = args.repo or config.repo
    subject = extract_subject(args.ticket, config.subject_patterns)
    scope = _store().get_scope(args.ticket, extract_verb(args.ticket), subject, repo=repo, before=_date(args.before))
    print(f"repo={repo} subject={subject!r} confidence={scope.confidence} matched={scope.matched} top_score={scope.top_score:.3f}")
    print(render_prompt(scope).strip())
    if scope.usable:
        print("\nProvenance:")
        for item in scope.files_changed + scope.lines:
            print(f"  {item.template}  <- {', '.join(item.sources)}")


def cmd_run(args: argparse.Namespace) -> None:
    from .agent import run_ticket

    summary = run_ticket(
        args.ticket, repo_root=args.repo_root, condition=args.condition, model=args.model, before=_date(args.before)
    )
    print(json.dumps(summary, indent=2, default=str))


def cmd_view(args: argparse.Namespace) -> None:
    from .viewer.app import watch

    watch(args.ticket, args.condition)


def cmd_mock(args: argparse.Namespace) -> None:
    from .viewer.mock_generate import run

    print(json.dumps(run(args.ticket, args.condition, args.delay), indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scope-envelope", description="Learned scopes for coding agents.")
    parser.add_argument("--repo-root", default=".", help="the repo to work in (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write .scope-envelope.toml with detected settings")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.set_defaults(func=cmd_init)

    sub.add_parser("setup", help="create the Atlas collections and Vector Search index").set_defaults(func=cmd_setup)

    learn = sub.add_parser("learn", help="mine merged, ticket-linked PRs into footprints")
    learn.add_argument("repo", nargs="?", help="owner/name (default: repo from .scope-envelope.toml)")
    learn.add_argument("--search", default="", help="extra GitHub search terms, e.g. 'label:enhancement'")
    learn.add_argument("--limit", type=int, default=200, help="how many merged PRs to read (default 200)")
    learn.add_argument("--max-files", type=int, default=25, help="skip PRs that change more files than this")
    learn.add_argument("--include-unlinked", action="store_true", help="also learn from PRs that don't close a ticket")
    learn.set_defaults(func=cmd_learn)

    scope = sub.add_parser("scope", help="show the scope learned for a ticket")
    scope.add_argument("ticket", help="ticket text, e.g. 'r/aws_sns_topic: Add foo argument'")
    scope.add_argument("--repo", help="owner/name (default: repo from .scope-envelope.toml)")
    scope.add_argument("--before", help="only learn from history before this date (YYYY-MM-DD)")
    scope.set_defaults(func=cmd_scope)

    run = sub.add_parser("run", help="run the agent on a ticket")
    run.add_argument("ticket", help="GitHub issue link, owner/repo#123, or ticket text")
    run.add_argument("--condition", choices=MODES, default="envelope")
    run.add_argument("--model", help="OpenRouter model id (default: config or MODEL env)")
    run.add_argument("--before", help="only learn from history before this date (YYYY-MM-DD)")
    run.set_defaults(func=cmd_run)

    view = sub.add_parser("view", help="live map of a run")
    view.add_argument("ticket", help="ticket id as the run logs it, e.g. owner/repo#123")
    view.add_argument("--condition", choices=MODES, default="envelope")
    view.set_defaults(func=cmd_view)

    mock = sub.add_parser("mock", help="scripted run on the toy fixture, for testing the viewer (writes to runs)")
    mock.add_argument("ticket")
    mock.add_argument("--condition", choices=MODES, default="envelope")
    mock.add_argument("--delay", type=float, default=1.5, help="seconds between scripted calls")
    mock.set_defaults(func=cmd_mock)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
