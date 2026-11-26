"""Check that a commit message follows Conventional Commits (D-033).

Used as a commit-msg hook: git passes the path of the message file. The header must read
`type(scope)!: description`, with the scope and `!` optional, at most 72 characters and no
trailing period. A body, if any, is separated from the header by a blank line. Messages git
writes itself (merges, reverts, fixup!/squash!/amend!) pass unchecked.

Exit code 1 prints what is wrong and the expected shape.
"""

import re
import sys
from pathlib import Path

TYPES = ("feat", "fix", "refactor", "perf", "test", "docs", "ci", "build", "chore", "revert")
MAX_HEADER = 72
HEADER = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([a-z0-9][a-z0-9-]*\))?!?: \S")
GIT_GENERATED = ("Merge ", 'Revert "', "fixup! ", "squash! ", "amend! ")
# `git commit -v` appends the diff below this line; git drops it from the message.
SCISSORS = "# ------------------------ >8 ------------------------"


def find_problems(message: str) -> list[str]:
    message = message.split(SCISSORS)[0]
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ["the message is empty"]
    header = lines[0]
    if header.startswith(GIT_GENERATED):
        return []
    problems = []
    if not HEADER.match(header):
        problems.append(f"header doesn't read `type(scope): description`: {header!r}")
    if len(header) > MAX_HEADER:
        problems.append(f"header is {len(header)} characters, over {MAX_HEADER}")
    if header.endswith("."):
        problems.append("header ends with a period")
    if len(lines) > 1 and lines[1].strip():
        problems.append("no blank line between the header and the body")
    return problems


def main(argv: list[str]) -> int:
    problems = find_problems(Path(argv[0]).read_text(encoding="utf-8"))
    if not problems:
        return 0
    for problem in problems:
        print(f"commit message: {problem}")
    print(f"expected `type(scope): description`, type one of: {', '.join(TYPES)}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
