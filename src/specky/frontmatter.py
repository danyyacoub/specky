"""Minimal frontmatter parser/writer for specs/<domain>/<topic>.md feature and workflow
docs. Hand-rolled rather than a YAML dependency — the schema is deliberately tiny (a
handful of scalar/list string fields), so a full YAML parser would be overkill.
"""

from __future__ import annotations

import re

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_LIST_VALUE = re.compile(r"^\[(.*)\]$")


def parse(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Split leading '---' frontmatter off a doc's content. Returns ({}, text) unchanged
    if there's no frontmatter block."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    meta: dict[str, str | list[str]] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        list_match = _LIST_VALUE.match(value)
        if list_match:
            meta[key] = [v.strip() for v in list_match.group(1).split(",") if v.strip()]
        else:
            meta[key] = value
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
