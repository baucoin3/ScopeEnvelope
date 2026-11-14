import os
import shutil
import argparse
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from scope_envelope import Gateway

load_dotenv()

# Fixed base prompt across both 'raw' and 'scoped' modes for fair comparison
BASE_PROMPT = """You are an autonomous coding agent. Solve the Jira ticket using the provided tools.
Rules:
- Never access files directly; always use the provided repository tools.
- Read relevant files first to understand existing conventions before writing code.
- Run tests using run_tests when you finish making changes.
"""

# Pricing per 1M tokens (adjust once Rami confirms the exact OpenRouter DeepSeek model)
INPUT_COST_PER_M = 0.14
OUTPUT_COST_PER_M = 0.28


def run_hidden_checks(repo_folder: str) -> dict:
    """Placeholder for Rami's hidden checks. Verifies the expected change was made."""
    registry_path = os.path.join(repo_folder, "registry.py")
    if os.path.exists(registry_path):
        with open(registry_path, "r") as f:
            content = f.read()
        if "OrderPlacedEvent" in content:
            return {"passed": 1, "total": 1}
    return {"passed": 0, "total": 1}


def load_ticket(ticket_id: str) -> tuple[str, str]:
    """Loads ticket text and source repo path. Replace with Rami's ticket loader when pushed."""
    sample_tickets = {
        "PIPE-2031": (
            "Implement Kafka consumer for OrderPlacedEvent and register it in registry.py.",
            "fake_repo"
        )
    }
    return sample_tickets.get(
        ticket_id,
        (f"Solve ticket {ticket_id}", "fake_repo")
    )


def run_agent(ticket_id: str, mode: str):
    ticket_text, source_repo = load_ticket(ticket_id)

    # Rule: Every run must start from a fresh, isolated copy of the repo
    work_repo = os.path.join("work", f"{ticket_id}-{mode}")
    if os.path.exists(work_repo):
        shutil.rmtree(work_repo)
    shutil.copytree(source_repo, work_repo)

    # Initialize Rami's Gateway for this run
    gw = Gateway(
        ticket_id=ticket_id,
        ticket_text=ticket_text,
        repo_root=work_repo,
        mode=mode,
    )

    # Build system prompt and retrieve the 6 gateway-wrapped tools
    scope_instructions = gw.scope_prompt()
    system_prompt = f"{BASE_PROMPT}\n{scope_instructions}".strip()
    tools = gw.tools()

    # Initialize DeepSeek via OpenRouter with fixed deterministic settings
    llm = ChatOpenAI(
        model="openrouter/free",
        openai_api_key=os.environ["OPENROUTER_API_KEY"],
        openai_api_base="https://openrouter.ai/api/v1",
        temperature=0,
    )

    agent = create_react_agent(llm, tools=tools)

    # Stream execution step-by-step to report token usage & cost after every LLM reply
    inputs = {
        "messages": [
            ("system", system_prompt),
            ("user", f"Ticket ({ticket_id}): {ticket_text}"),
        ]
    }

    for event in agent.stream(inputs, config={"recursion_limit": 60}, stream_mode="updates"):
        if "agent" in event:
            for msg in event["agent"]["messages"]:
                if hasattr(msg, "usage_metadata") and msg.usage_metadata:
                    prompt_tokens = msg.usage_metadata.get("input_tokens", 0)
                    completion_tokens = msg.usage_metadata.get("output_tokens", 0)
                    cost_usd = (
                        prompt_tokens * INPUT_COST_PER_M
                        + completion_tokens * OUTPUT_COST_PER_M
                    ) / 1_000_000

                    gw.record_llm_step(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost_usd=cost_usd,
                    )

    # Run hidden checks against the isolated work repo (never ask the LLM to grade itself)
    checks = run_hidden_checks(work_repo)

    # Finalize run and save footprint in Atlas if checks passed
    summary = gw.finish(checks=checks)
    print(f"\n=== Run Complete ({mode}) ===")
    print(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ScopeEnvelope coding agent on a ticket.")
    parser.add_argument("ticket_id", nargs="?", help="Ticket ID to run (e.g., PIPE-2031)")
    parser.add_argument("--mode", choices=["raw", "scoped"], help="Execution mode (raw or scoped)")
    args = parser.parse_args()

    # Interactive prompt if arguments are not passed in the CLI (great for live demos)
    ticket_id = args.ticket_id or input("Enter Ticket ID (e.g., PIPE-2031): ").strip()
    mode = args.mode
    if not mode:
        entered_mode = input("Enter mode [raw/scoped] (default: scoped): ").strip().lower()
        mode = entered_mode if entered_mode in ["raw", "scoped"] else "scoped"

    run_agent(ticket_id=ticket_id, mode=mode)

# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(description="Run ScopeEnvelope coding agent on a ticket.")
#     parser.add_argument("ticket_id", help="Ticket ID to run (e.g., PIPE-2031)")
#     parser.add_argument("--mode", choices=["raw", "scoped"], required=True, help="Execution mode")
#     args = parser.parse_args()

#     run_agent(ticket_id=args.ticket_id, mode=args.mode)

    