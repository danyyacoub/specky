"""`specky cost` — what specky has asked a provider to do, from the rows CachingProvider writes.

Characters, not dollars and not tokens: specky knows neither the provider's tokenizer nor its price
list, and both change under it. A figure it can't stand behind would be worse than an honest one, so
this reports call counts, the cache hit rate, and prompt/response character totals — enough to see
that a re-run cost nothing, or that one command is doing all the spending.

Grouped by command *and* model, because those are the two things a user can act on: which part of
specky to run less often, and which model to point it at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from specky.db import connect


@dataclass(frozen=True)
class Group:
    command: str
    model: str
    calls: int
    cached: int
    prompt_chars: int
    response_chars: int

    @property
    def hit_rate(self) -> int:
        return round(100 * self.cached / self.calls) if self.calls else 0

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "model": self.model,
            "calls": self.calls,
            "cached": self.cached,
            "hit_rate": self.hit_rate,
            "prompt_chars": self.prompt_chars,
            "response_chars": self.response_chars,
        }


@dataclass(frozen=True)
class Report:
    groups: tuple[Group, ...]
    cache_entries: int
    cache_chars: int
    since: str | None = None

    @property
    def total(self) -> Group:
        return Group(
            command="TOTAL",
            model="",
            calls=sum(g.calls for g in self.groups),
            cached=sum(g.cached for g in self.groups),
            prompt_chars=sum(g.prompt_chars for g in self.groups),
            response_chars=sum(g.response_chars for g in self.groups),
        )

    def as_dict(self) -> dict:
        return {
            "since": self.since,
            "groups": [g.as_dict() for g in self.groups],
            "total": self.total.as_dict(),
            "cache": {"entries": self.cache_entries, "chars": self.cache_chars},
        }


def run_cost(repo_root: Path, since: str | None = None) -> Report:
    """Aggregate in SQL rather than in Python: a long backfill writes a row per call, and there's no
    reason to carry tens of thousands of them into the process to add six numbers up.

    `since` is compared as text against the stored UTC ISO timestamps, so a plain `2026-09-01` is a
    valid prefix and needs no date parsing — and a full timestamp works for the same reason.
    """
    where = " WHERE created_at >= ?" if since else ""
    params = (since,) if since else ()
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT command, model, COUNT(*), SUM(cached), SUM(prompt_chars), SUM(response_chars) "
            f"FROM usage{where} GROUP BY command, model ORDER BY COUNT(*) DESC, command, model",
            params,
        ).fetchall()
        entries, chars = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(response)), 0) FROM prompt_cache"
        ).fetchone()
    finally:
        conn.close()
    return Report(
        groups=tuple(
            Group(command or "(unknown)", model, calls, cached, prompt, response)
            for command, model, calls, cached, prompt, response in rows
        ),
        cache_entries=entries,
        cache_chars=chars,
        since=since,
    )


def clear_cache(repo_root: Path) -> int:
    """Drop every memoized response, returning how many went. The usage rows stay: they're the
    record of what was spent, and deleting them would make the cache look free in hindsight."""
    conn = connect(repo_root)
    try:
        count = conn.execute("SELECT COUNT(*) FROM prompt_cache").fetchone()[0]
        conn.execute("DELETE FROM prompt_cache")
        conn.commit()
        return count
    finally:
        conn.close()


def _thousands(n: int) -> str:
    return f"{n:,}"


def report_lines(report: Report) -> list[str]:
    if not report.groups:
        return [
            "specky cost: no provider calls recorded"
            + (f" since {report.since}" if report.since else "")
            + " — the usage log starts the first time specky calls a model"
        ]
    header = ("command", "model", "calls", "cached", "prompt chars", "response chars")
    rows = [
        (
            g.command,
            g.model,
            _thousands(g.calls),
            f"{g.cached} ({g.hit_rate}%)",
            _thousands(g.prompt_chars),
            _thousands(g.response_chars),
        )
        for g in (*report.groups, report.total)
    ]
    widths = [max(len(r[i]) for r in (header, *rows)) for i in range(len(header))]
    lines = [
        "specky cost: provider calls" + (f" since {report.since}" if report.since else ""),
        "",
        "  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    # The total is one of the rows above, separated by a rule so it doesn't read as another command.
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows[:-1]]
    lines.append("  ".join("-" * w for w in widths))
    lines.append("  ".join(c.ljust(w) for c, w in zip(rows[-1], widths)).rstrip())
    lines.append("")
    lines.append(
        f"Cache: {_thousands(report.cache_entries)} response(s), "
        f"{_thousands(report.cache_chars)} chars — `specky cost --clear-cache` empties it"
    )
    return lines
