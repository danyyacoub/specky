"""Reading `git log` in bulk, shared by the indexer and the staleness pass.

Both need the same trick: one `git log --name-only` process answering for many commits at once,
rather than a git process per commit or per file. A repo specky is installed into can have a
hundred thousand commits, so "one process per X" is the thing to avoid, and `--name-only` output
is only parseable if the commit boundaries are unambiguous — hence the `\\x01` record separator
below, which no path can contain.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Record separator for `--format=%x01…`. A filename can contain anything a newline can't, so a
# blank-line-delimited parse is guessable at best; \x01 can't appear in a path at all.
RECORD = "\x01"


def run(repo_root: Path, args: list[str], stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, input=stdin, capture_output=True, text=True, check=True
    ).stdout


def blocks(log: str) -> list[list[str]]:
    """`--format=%x01…` output as one line list per commit: `[format line, *file paths]`."""
    return [lines for block in log.split(RECORD) if (lines := block.splitlines())]
