#!/usr/bin/env python3
"""CLI: python run.py "<ticket text>" --condition lazy|envelope"""

import argparse
import json

from agent import run_ticket


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a ticket against the toy repo agent.")
    parser.add_argument("ticket", help="Ticket text describing the change to make.")
    parser.add_argument(
        "--condition",
        choices=["lazy", "envelope"],
        default="lazy",
        help="Prompt condition: lazy (base prompt only) or envelope (base + scope).",
    )
    args = parser.parse_args()

    record = run_ticket(args.ticket, args.condition)
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
