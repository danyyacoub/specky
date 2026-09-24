"""Minimal frontmatter parser/writer for specs/<domain>/<topic>.md feature and workflow
docs. Hand-rolled rather than a YAML dependency — the schema is deliberately tiny (a
handful of scalar/list string fields), so a full YAML parser would be overkill.
"""

from __future__ import annotations

import re

# The `\n?\n?` after the closing fence swallows the blank line render() writes there, so
# parse(render(meta, body)) gives back exactly `body` — without it every parse/render cycle
# (e.g. `specky sync` re-tagging an existing doc) prepended another blank line to the doc.
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?\n?(.*)$", re.DOTALL)
_LIST_VALUE = re.compile(r"^\[(.*)\]$")
# A frontmatter line: a bare identifier-ish key, then a colon. Requiring *every* non-blank
# line in the block to look like this is what keeps a doc that merely opens with a `---`
# thematic break from being read as frontmatter — otherwise the next `---` in the file closes
# a block that never opened, and the prose between them is swallowed into `meta`.
_META_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:")
# An item of a YAML block list (`sources:` then `  - api/x.py` lines). Agents write lists this
# way as often as inline, and a block the parser can't read loses the doc's whole frontmatter.
_BLOCK_ITEM = re.compile(r"^\s+-\s+(.*)$|^-\s+(.*)$")


def parse(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Split leading '---' frontmatter off a doc's content. Returns ({}, text) unchanged
    if there's no frontmatter block."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    lines = [line for line in match.group(1).splitlines() if line.strip()]
    if not lines or not _META_LINE.match(lines[0]):
        return {}, text

    meta: dict[str, str | list[str]] = {}
    block_key: str | None = None  # the key whose `- item` lines are being read
    opened: set[str] = set()  # keys with no inline value, which may or may not get items
    for line in lines:
        item = _BLOCK_ITEM.match(line)
        if item and block_key is not None:
            value = (item.group(1) or item.group(2) or "").strip().strip("'\"")
            if value:
                meta[block_key].append(value)  # type: ignore[union-attr]
            continue
        if not _META_LINE.match(line):
            return {}, text
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        block_key = None
        list_match = _LIST_VALUE.match(value)
        if list_match:
            meta[key] = [v.strip() for v in list_match.group(1).split(",") if v.strip()]
        elif not value:
            meta[key] = []
            block_key = key
            opened.add(key)
        else:
            meta[key] = value
    # A bare `key:` with no items under it was a scalar left empty, not a list.
    for key in opened:
        if meta[key] == []:
            meta[key] = ""
    return meta, match.group(2)


def render(meta: dict[str, str | list[str]], body: str) -> str:
    """Inverse of parse(): prepend a '---' frontmatter block for the given metadata.
    Returns body unchanged if meta is empty."""
    if not meta:
        return body
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body
