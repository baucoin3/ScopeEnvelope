"""scope-agent: an interactive coding agent session with the ScopeEnvelope gateway as a switch.

    $ scope-agent
    > Please take a look at this ticket https://github.com/owner/repo/issues/123
    > Enable ScopeEnvelope gateway
    > Please take a look at this ticket https://github.com/owner/repo/issues/123
"""

from __future__ import annotations

import os
import re
import uuid

from rich.console import Console

from .config import load_config, load_env

_LINK = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/(?:issues|pull)/\d+|\b[\w.-]+/[\w.-]+#\d+\b")
_ON = re.compile(r"^/scope(?:envelope)?(?:\s+on)?$|\b(enable|turn on|switch on|activate|start)\b.*\b(scope\s*envelope|gateway)\b", re.I)
_OFF = re.compile(r"^/scope(?:envelope)?\s+off$|\b(disable|turn off|switch off|deactivate|stop)\b.*\b(scope\s*envelope|gateway)\b", re.I)


def parse(message: str) -> tuple[str, str | None, str]:
    """-> (intent, ticket link, remaining text). intent: quit | help | on | off | run | chat."""
    text = message.strip()
    if text.lower() in ("/exit", "/quit", "exit", "quit", "q"):
        return "quit", None, ""
    if text.lower() in ("/help", "help", "?"):
        return "help", None, ""
    link = _LINK.search(text)
    if link:
        return "run", link.group(0), (text[: link.start()] + text[link.end():]).strip()
    if _OFF.search(text):
        return "off", None, ""
    if _ON.search(text):
        return "on", None, ""
    return "chat", None, text


def event_line(e: dict) -> str | None:
    """One friendly line per event, or None for events that would only be noise in the terminal."""
    tool, decision, path = e.get("tool"), e.get("decision"), e.get("path")
    if tool == "learn":
        return {
            "start": f"[magenta]No recent history for {e.get('repo')} in Atlas: learning it first...[/]",
            "progress": f"[magenta]  learned {e.get('done')}/{e.get('total')} tickets[/]",
            "done": f"[magenta]✓ learned {e.get('saved')} tickets into Atlas[/]",
            "fresh": f"[magenta]Using {e.get('footprints')} tickets already learned in Atlas[/]",
        }.get(decision)
    if tool == "scope" and e.get("condition") == "envelope":
        scope = e.get("scope") or {}
        if scope.get("confidence") in ("strong", "weak"):
            return f"[magenta]Scope learned from {', '.join(scope.get('based_on') or [])} ({scope['confidence']} match)[/]"
        return "[yellow]No learned scope for this ticket yet: writes will need approval[/]"
    if tool == "read_file":
        return f"  [blue]read[/]  {path}"
    if tool == "write_file":
        if decision == "blocked":
            return f"  [red]write {path} ✗ blocked[/] ({e.get('reason')})"
        return f"  [green]write {path} ✓[/] (+{e.get('lines_added', 0)}/-{e.get('lines_removed', 0)})"
    if tool == "request_write":
        return f"  [yellow]asked to write {path} → {decision}[/]"
    if tool == "run_tests":
        return "  [dim]ran tests[/]"
    return None


def compare(now: dict, before: dict | None) -> str:
    if not before:
        return ""

    def pct(key: str) -> str:
        a, b = before.get(key) or 0, now.get(key) or 0
        return f"{(b - a) / a * 100:+.0f}%" if a else "n/a"

    return (
        f"vs {before['condition']}: {pct('calls')} calls, {pct('prompt_tokens')} prompt tokens, "
        f"{pct('cost_usd')} cost, {pct('files_read')} files read"
    )


HELP = """Paste a ticket link (GitHub issue) and I'll start working on it.
  enable ScopeEnvelope gateway   /scopeenvelope on    turn the gateway on
  disable ScopeEnvelope gateway  /scopeenvelope off   turn it off
  /exit                                               leave"""


def main() -> None:
    load_env()
    console = Console()
    config = load_config(".")
    model = os.environ.get("MODEL", config.model)
    ui_url = os.environ.get("SCOPE_UI_URL", "")
    if not ui_url and os.environ.get("WEBVIZ", "1") != "0":
        try:  # start the live browser view alongside the session
            from .webviz.server import serve_in_background

            ui_url = serve_in_background(os.environ.get("WEBVIZ_HOST", "127.0.0.1"), int(os.environ.get("WEBVIZ_PORT", "8765")))
        except Exception as exc:  # the terminal session still works without the browser view
            console.print(f"[yellow]Live browser view not started: {exc}[/]")
    gateway_on = False
    last: dict[str, dict] = {}  # ticket id -> last summary, for the comparison line

    try:
        import readline  # noqa: F401  (arrow keys and history in input())
    except ImportError:
        pass

    def header() -> None:
        state = "[bold green]ON[/]" if gateway_on else "[bold red]OFF[/]"
        console.rule(f"scope-agent · {config.repo or 'local repo'} · {model} · ScopeEnvelope gateway: {state}")

    header()
    if ui_url:
        console.print(f"[bold]Live view:[/] {ui_url}")
    console.print("Hi! How can I help you today? [dim](paste a ticket link, or type /help)[/]")
    while True:
        try:
            message = console.input("\n[bold cyan]> [/]")
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            return
        intent, link, rest = parse(message)
        if intent == "quit":
            console.print("Bye!")
            return
        if intent == "help":
            console.print(HELP)
        elif intent in ("on", "off"):
            gateway_on = intent == "on"
            console.print(f"✓ ScopeEnvelope gateway {'ON' if gateway_on else 'OFF'}")
            header()
        elif intent == "chat":
            console.print("Paste a ticket link and I'll start working on it. [dim](/help for commands)[/]")
        else:
            _run(console, link, rest, gateway_on, ui_url, last)


def _run(console: Console, link: str, instructions: str, gateway_on: bool, ui_url: str, last: dict) -> None:
    from .agent import run_ticket

    condition = "envelope" if gateway_on else "lazy"
    run_id = uuid.uuid4().hex[:12]
    live = ui_url.replace("{run_id}", run_id) if ui_url else f"run_id {run_id}"
    console.print(f"Working on {link} ({'ScopeEnvelope gateway' if gateway_on else 'no gateway'})")
    console.print(f"[bold]Live view:[/] {live}")

    def show(event: dict) -> None:
        line = event_line(event)
        if line:
            console.print(line)

    try:
        with console.status("Agent working...", spinner="dots"):
            summary = run_ticket(
                link, repo_root=".", condition=condition, verbose=False,
                run_id=run_id, instructions=instructions, on_event=show,
            )
    except KeyboardInterrupt:
        console.print("[yellow]Stopped.[/]")
        return
    except SystemExit as exc:
        console.print(f"[red]{exc}[/]")
        return
    except Exception as exc:  # keep the session alive
        console.print(f"[red]Run failed: {type(exc).__name__}: {exc}[/]")
        return

    console.print(
        f"[bold green]✓ Done[/]: {summary['calls']} calls · {summary['files_read']} files read · "
        f"{summary['prompt_tokens']:,} prompt tokens · ${summary['cost_usd']:.4f}"
        + (f" · {summary['blocked']} blocked" if summary.get("blocked") else "")
    )
    previous = last.get(summary["ticket"])
    if previous and previous["condition"] != summary["condition"]:
        console.print(f"[bold]{compare(summary, previous)}[/]")
    if summary.get("final_answer"):
        console.print(f"[dim]{summary['final_answer'][:600]}[/]")
    last[summary["ticket"]] = summary


if __name__ == "__main__":
    main()
