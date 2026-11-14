"""The coding agent: a LangChain agent whose every tool call goes through a Gateway.

    from scope_envelope.agent import run_ticket
    summary = run_ticket("https://github.com/owner/repo/issues/123", repo_root=".", condition="envelope")

Needs the `agent` extra (langchain, langchain-openai) and OPENROUTER_API_KEY.
"""

from __future__ import annotations

import os
from datetime import datetime

from dotenv import find_dotenv, load_dotenv
from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI

from .config import load_config
from .gateway import Gateway
from .tickets import fetch_ticket

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
    # usage.include asks OpenRouter to return the billed cost with every reply.
    return ChatOpenAI(model=model, api_key=api_key, base_url=OPENROUTER_URL, extra_body={"usage": {"include": True}})


def run_ticket(
    ticket: str,
    *,
    repo_root: str = ".",
    condition: str = "envelope",
    model: str | None = None,
    before: datetime | None = None,
    verbose: bool = True,
) -> dict:
    """Run one ticket (a GitHub issue link, owner/repo#123, or plain text) and return the run summary."""
    load_dotenv(find_dotenv(usecwd=True))
    config = load_config(repo_root)
    llm = build_model(model or config.model)  # fail on a missing API key before anything is logged
    ticket_id, ticket_text = fetch_ticket(ticket)
    gw = Gateway(
        ticket_id, ticket_text, repo_root, condition, config=config, model=model, before=before, verbose=verbose
    )
    agent = create_agent(model=llm, tools=gw.tools(), system_prompt=BASE_SYSTEM_PROMPT + gw.scope_prompt())
    final_answer = ""
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": ticket_text}]},
            # One tool call is a model step plus a tool step, so allow about two graph steps per call.
            config={"recursion_limit": 2 * config.max_calls + 5, "callbacks": [UsageReporter(gw)]},
        )
        messages = result.get("messages", [])
        final_answer = str(messages[-1].content) if messages else ""
    except Exception as exc:  # hitting the step limit or a model error still ends with checks and a summary
        final_answer = f"Run stopped: {type(exc).__name__}: {exc}"
    summary = gw.finish(gw.run_checks())
    summary["final_answer"] = final_answer
    return summary
