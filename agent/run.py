"""Kept so `python -m agent.run` still works. The runner now lives in the package:

    scope-envelope run <ticket> --condition lazy|envelope

That shared runner includes this branch's ideas: a fresh copy of the repo per run,
temperature 0 and interactive prompts. It also adds DeepSeek V4 Pro by default,
OpenRouter's billed cost per step, ticket links and the repo's own test command.
"""

import sys

from scope_envelope.cli import main

MODE_ALIASES = {"raw": "lazy", "scoped": "envelope"}  # names used before the rename


def _translate(argv: list[str]) -> list[str]:
    out, i = ["run"], 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--mode" and i + 1 < len(argv):
            out += ["--condition", MODE_ALIASES.get(argv[i + 1], argv[i + 1])]
            i += 2
            continue
        out.append(arg)
        i += 1
    return out


if __name__ == "__main__":
    main(_translate(sys.argv[1:]))
