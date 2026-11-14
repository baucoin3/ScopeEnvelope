"""The coding agent: a LangChain agent whose every tool call goes through a Gateway.

    from scope_envelope.agent import run_ticket
    summary = run_ticket("https://github.com/owner/repo/issues/123", repo_root=".", condition="envelope")

Needs the `agent` extra (langchain, langchain-openai) and OPENROUTER_API_KEY.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI

from . import workspace
from .config import Config, load_config
from .gateway import Gateway, extract_verb
from .store import Store
from .tickets import Ticket, fetch_ticket

log = logging.getLogger("scope_envelope")

OPENROUTER_URL = "https://openrouter.ai/api/v1"

# USD per 1M tokens (input, output). Only used when OpenRouter doesn't report the billed cost.
PRICES = {
    "deepseek/deepseek-v4-pro": (0.90, 1.50),
    "deepseek/deepseek-v4.1-flash-1017": (0.038, 0.50),
    "deepseek/deepseek-v4.1-flash": (0.0507, 0.2028),
}

BASE_SYSTEM_PROMPT = (
    "You are a coding agent working inside a single repository. "
    "You are given a ticket describing a change to make. "
    "Use the available tools to explore the repo, read relevant files, "
    "make the smallest edit that satisfies the ticket, and verify your "
    "change with run_tests. All paths are relative to the repo root. "
    "When you are done, summarize what you changed in your final answer."
)


def price(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    rates = PRICES.get(model)
    if not rates:
        return None
    return (prompt_tokens * rates[0] + completion_tokens * rates[1]) / 1_000_000


class UsageReporter(BaseCallbackHandler):
    """Reports tokens and cost to the gateway after every model reply, so the viewer updates live."""

    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        usage = dict((response.llm_output or {}).get("token_usage") or {})
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None:  # some providers only fill usage_metadata on the message
            message = getattr(response.generations[0][0], "message", None) if response.generations else None
            meta = getattr(message, "usage_metadata", None) or {}
            prompt, completion = meta.get("input_tokens", 0), meta.get("output_tokens", 0)
        prompt, completion = int(prompt or 0), int(completion or 0)
        cost = usage.get("cost")
        if cost is not None:
            self.gateway.record_llm_step(prompt, completion, float(cost), "openrouter_billed")
            return
        estimate = price(self.gateway.model, prompt, completion)
        self.gateway.record_llm_step(prompt, completion, estimate, "price_table" if estimate is not None else "unknown")


def build_model(model: str) -> ChatOpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set. Add it to .env (see .env.example).")
    # temperature=0 keeps lazy vs envelope comparisons as repeatable as the model allows.
    # usage.include asks OpenRouter to return the billed cost with every reply.
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=OPENROUTER_URL,
        temperature=0,
        extra_body={"usage": {"include": True}},
    )


def _event(store: Store | None, run_id: str, ticket: Ticket, condition: str, repo: str | None, **fields) -> None:
    """A run-level event in `runs`, outside any Gateway (used while learning, before the run starts)."""
    if store is None:
        return
    try:
        store.log({
            "run_id": run_id, "ticket": ticket.id, "repo": repo, "condition": condition,
            "tool": "learn", "ts": datetime.now(UTC), **fields,
        })
    except Exception as exc:  # telemetry must not stop the run
        log.warning("could not log learn event: %s", exc)


def auto_learn(store: Store, config: Config, ticket: Ticket, condition: str, run_id: str) -> dict:
    """Learn the repo first when Atlas has no recent history for it. Returns what happened."""
    from .mine import learn, needs_learning

    repo = config.repo
    status = store.repo_status(repo)
    if not needs_learning(status, config.learn_max_age_days):
        _event(store, run_id, ticket, condition, repo, decision="fresh", footprints=status.get("footprints"))
        return {"learned": False, "reason": "history is recent", "footprints": status.get("footprints")}

    print(f"[scope] No recent history for {repo} in Atlas. Learning it first ...", file=sys.stderr)
    _event(store, run_id, ticket, condition, repo, decision="start", limit=config.learn_limit)
    store.ensure_indexes()
    own_ticket = ticket.number if ticket.repo == repo else None

    def progress(done: int, total: int) -> None:
        if done == total or done % 10 == 0:
            print(f"[scope] learned {done}/{total} tickets", file=sys.stderr)
            _event(store, run_id, ticket, condition, repo, decision="progress", done=done, total=total)

    report = learn(
        repo, store, limit=config.learn_limit, exclude_issue=own_ticket,
        before=ticket.created_at if own_ticket else None, on_progress=progress,
    )
    _event(store, run_id, ticket, condition, repo, decision="done", saved=report.saved)
    print(f"[scope] saved {report.saved} footprints; waiting for Atlas to index them ...", file=sys.stderr)
    store.wait_until_queryable(timeout_s=120)
    for _ in range(24):  # Atlas embeds new documents asynchronously; wait until search sees them
        if store.footprints.count_documents({"repo": repo}) and _searchable(store, ticket, repo, own_ticket):
            break
        time.sleep(5)
    return {"learned": True, "saved": report.saved}


def _searchable(store: Store, ticket: Ticket, repo: str, own_ticket: int | None) -> bool:
    try:
        scope = store.get_scope(ticket.text, extract_verb(ticket.text), None, repo=repo, exclude_issue=own_ticket)
        return scope.matched > 0
    except Exception:  # index still building
        return False


def run_ticket(
    ticket: str,
    *,
    repo_root: str = ".",
    condition: str = "envelope",
    model: str | None = None,
    before: datetime | None = None,
    base: str = "HEAD",
    in_place: bool = False,
    verbose: bool = True,
) -> dict:
    """Run one ticket (a GitHub issue link, owner/repo#123, or plain text) and return the run summary.

    By default the agent works in a fresh copy of the repo at `base` (see workspace.py), so
    lazy and envelope runs start from the same state and your checkout is never touched.
    Envelope runs learn the repo's history first when Atlas has none (or it is stale).
    """
    load_dotenv(find_dotenv(usecwd=True))
    config = load_config(repo_root)
    llm = build_model(model or config.model)  # fail on a missing API key before anything is logged
    t = fetch_ticket(ticket)
    store = Store.from_env()
    run_id = uuid.uuid4().hex[:12]

    learned = None
    if condition == "envelope" and store and config.repo and config.auto_learn:
        learned = auto_learn(store, config, t, condition, run_id)

    work_root = Path(repo_root).resolve() if in_place else workspace.prepare(repo_root, t.id, condition, base)
    gw = Gateway(
        t.id, t.text, work_root, condition, config=config, store=store, model=model, before=before,
        exclude_issue=t.number if t.repo == config.repo else None, run_id=run_id, verbose=verbose,
    )
    agent = create_agent(model=llm, tools=gw.tools(), system_prompt=BASE_SYSTEM_PROMPT + gw.scope_prompt())
    final_answer = ""
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": t.text}]},
            # One tool call is a model step plus a tool step, so allow about two graph steps per call.
            config={"recursion_limit": 2 * config.max_calls + 5, "callbacks": [UsageReporter(gw)]},
        )
        messages = result.get("messages", [])
        final_answer = str(messages[-1].content) if messages else ""
    except Exception as exc:  # hitting the step limit or a model error still ends with checks and a summary
        final_answer = f"Run stopped: {type(exc).__name__}: {exc}"
    summary = gw.finish(gw.run_checks())
    summary["workspace"] = str(work_root)
    summary["learned"] = learned
    summary["final_answer"] = final_answer
    return summary
