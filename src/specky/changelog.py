"""The changelog: what changed in the product, one entry per day, newest first.

Deploys can be frequent and say nothing, so neither a deploy nor a release is the unit. A **day**
is: everything user-facing whose history doc is dated that day, under one heading, behaviour
changes that need the reader to adapt (`breaking`) first. Days with only `internal` work don't
appear. A release tag cut that day is a marker on the day, never an entry of its own.

**Read from the history docs, not git.** Each doc carries its date, authors, impact, the docs it
touched and its words (headline, What changed, Example), so the changelog is the same on a laptop
and on a deployed server whose checkout is the docs copied into a fresh `git init`. Only the
release markers come from git, and a checkout without the tags simply has none. No model is called.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path

from specky import gitlog, paths
from specky.commit_doc import IMPACTS, brief, doc_stamp, read_history

DEFAULT_DAYS = 90  # what the full page reaches back over
DEFAULT_HOME_DAYS = 7  # what the home page shows
DEFAULT_TAG_PATTERN = "v*"

# Within a day: what a reader must act on first, `IMPACTS` order, then anything unclassified.
_IMPACT_ORDER = {impact: i for i, impact in enumerate(IMPACTS)}


@dataclass(frozen=True)
class ChangelogConfig:
    days: int = DEFAULT_DAYS
    home_days: int = DEFAULT_HOME_DAYS
    tag_pattern: str = DEFAULT_TAG_PATTERN
    enabled: bool = True

    @classmethod
    def load(cls, repo_root: Path) -> ChangelogConfig:
        """`[changelog]` in specky.toml, or `[tool.specky.changelog]` in pyproject.toml."""
        table = paths.read_table(repo_root / "specky.toml", ("changelog",)) or paths.read_table(
            repo_root / "pyproject.toml", ("tool", "specky", "changelog")
        )
        return cls(
            days=max(1, int(table.get("days", DEFAULT_DAYS))),
            home_days=max(1, int(table.get("home_days", DEFAULT_HOME_DAYS))),
            tag_pattern=str(table.get("tag_pattern", DEFAULT_TAG_PATTERN)),
            enabled=bool(table.get("enabled", True)),
        )


@dataclass(frozen=True)
class Entry:
    """One history doc's change, as the changelog tells it."""

    path: str  # the history doc, repo-relative
    when: datetime
    headline: str
    impact: str
    what: str
    scenario: str
    before: str
    after: str
    features: tuple[str, ...]
    authors: tuple[str, ...]  # names


@dataclass
class Day:
    day: date
    entries: list[Entry] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)  # tags cut that day, newest first

    @property
    def anchor(self) -> str:
        return self.day.isoformat()


def entries(repo_root: Path, since: datetime) -> list[Entry]:
    """Every user-facing history doc dated at or after `since`, newest first."""
    history_dir = paths.history_dir(repo_root)
    if not history_dir.is_dir():
        return []
    found: list[Entry] = []
    for path in history_dir.glob("*.md"):
        text = path.read_text(errors="replace")
        parsed = read_history(text)
        when, authors = doc_stamp(text)
        if parsed is None or when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < since:
            continue
        _, doc = parsed
        if doc.impact == "internal":
            continue
        found.append(
            Entry(
                path=path.relative_to(repo_root).as_posix(),
                when=when,
                headline=brief(doc),
                impact=doc.impact,
                # A legacy doc's paragraph is its whole text, and its first sentence the headline.
                what=doc.what if doc.headline else "",
                scenario=doc.scenario,
                before=doc.before,
                after=doc.after,
                features=tuple(doc.features),
                authors=tuple(dict.fromkeys(name for name, _ in authors)),
            )
        )
    found.sort(key=lambda e: e.when, reverse=True)
    return found


def releases(repo_root: Path, pattern: str = DEFAULT_TAG_PATTERN) -> dict[date, list[str]]:
    """`{day: [tags cut that day]}` for the tags matching `pattern`, newest first within a day.
    Empty when git has no tags or isn't there — markers are a nicety, never a reason to fail."""
    try:
        out = gitlog.run(
            repo_root,
            [
                "for-each-ref",
                "--sort=-creatordate",
                "--format=%(refname:short)%1f%(creatordate:iso-strict)",
                "refs/tags",
            ],
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    found: dict[date, list[str]] = {}
    for line in out.splitlines():
        name, _, stamp = line.partition("\x1f")
        if not name or not fnmatch(name, pattern):
            continue
        try:
            when = datetime.fromisoformat(stamp.strip())
        except ValueError:
            continue
        found.setdefault(when.date(), []).append(name)
    return found


def days(
    repo_root: Path,
    window: int,
    now: datetime | None = None,
    cfg: ChangelogConfig | None = None,
) -> list[Day]:
    """The last `window` days that have user-facing changes, newest first. A day is the date the
    change's doc gives, in that doc's own time zone: the day its author saw it happen."""
    cfg = cfg or ChangelogConfig.load(repo_root)
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window)
    tags = releases(repo_root, cfg.tag_pattern)
    by_day: dict[date, Day] = {}
    for entry in entries(repo_root, since):
        day = entry.when.date()
        by_day.setdefault(day, Day(day=day, releases=tags.get(day, []))).entries.append(entry)
    for day in by_day.values():
        day.entries.sort(key=lambda e: (_IMPACT_ORDER.get(e.impact, len(IMPACTS)), -e.when.timestamp()))
    return sorted(by_day.values(), key=lambda d: d.day, reverse=True)
