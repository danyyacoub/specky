"""`specky check` — the CI gate: did this change's code touch a doc that describes it?

No AI provider is ever called and no history is walked, so this is free and fast enough to run
on every pull request. It answers from two things that already exist: the diff for the range,
and the `doc_files` table `specky index` derives from `commit_links` (see
indexer.index_doc_files). A violation is a code file whose covering doc no commit in the range
updated.

Fails by default — a doc gate that only warns is a doc gate nobody notices — with `--advisory`
to print the identical report and exit 0, which is how a repo adopts this before it's clean.
Everything else it reports (commits with no history doc, changed files with no doc at all, the
coverage figure) is advice and never affects the exit code: those are states a repo grows into,
not regressions a contributor introduced.
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from specky.commit_doc import _AUTO_COMMIT_MARKER, _is_revision, history_doc_for
from specky.db import connect

# git's hash of the empty tree: diffing against it yields the whole worktree, which is what a
# repo with a single commit (or `--since` covering all of history) has to compare against.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Paths whose changes never demand a doc update. Overridable wholesale via `[check] ignore`.
# `*.md` here only ever sees markdown *outside* specs/ — the specs/ paths in a diff are the
# doc updates being checked for, and are partitioned off before this list is consulted.
DEFAULT_IGNORE = (
    ".github/",
    ".specky/",  # specky's own derived index and rendered site, if a repo forgot to gitignore it
    "*.md",
    "*.lock",
    "*-lock.json",
    "*.txt",
    "*.cfg",
    "*.ini",
)


# How many separate doc-producing commits must pair a file with a doc before failing the build
# over it. One commit is not evidence: `commit_links` records that a commit produced a doc, and
# `doc_files` then pairs that doc with *every* file in the commit — the incidental test file and
# CLI plumbing along with the code the doc is actually about. A second, independent commit
# pairing the same two is what distinguishes a doc that tracks a file from a coincidence. Pairs
# below the threshold still count as coverage; they just don't fail anything.
MIN_LINK_COMMITS = 2


def _read_table(path: Path, keys: tuple[str, ...]) -> dict:
    """A nested TOML table, or `{}` if the file or any key along the way is absent."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        table = tomllib.load(f)
    for key in keys:
        table = table.get(key) or {}
    return table


@dataclass(frozen=True)
class CheckConfig:
    ignore: tuple[str, ...] = DEFAULT_IGNORE
    min_link_commits: int = MIN_LINK_COMMITS

    @classmethod
    def load(cls, repo_root: Path) -> CheckConfig:
        """`[check]` in specky.toml, or `[tool.specky.check]` in pyproject.toml.

        The second spelling exists because `specky init` writes a *gitignored* specky.toml — it
        holds provider config — so a policy kept only there is absent in CI, which is the one place
        this gate runs. pyproject.toml is committed, so that's where a repo can put a gate policy
        its whole team and its CI share. specky.toml wins when it has a `[check]` table, so a
        developer can still override locally.
        """
        table = _read_table(repo_root / "specky.toml", ("check",)) or _read_table(
            repo_root / "pyproject.toml", ("tool", "specky", "check")
        )
        ignore = table.get("ignore")
        return cls(
            ignore=(
                DEFAULT_IGNORE
                if ignore is None
                else tuple([ignore] if isinstance(ignore, str) else ignore)
            ),
            min_link_commits=max(1, int(table.get("min_link_commits", MIN_LINK_COMMITS))),
        )

    def ignores(self, path: str) -> bool:
        return any(
            path.startswith(pat) if pat.endswith("/") else fnmatch(path, pat)
            for pat in self.ignore
        )


@dataclass(frozen=True)
class Violation:
    path: str
    doc_path: str

    def as_dict(self) -> dict:
        return {"path": self.path, "doc_path": self.doc_path}


@dataclass(frozen=True)
class Report:
    base: str
    code_files: tuple[str, ...]
    touched_docs: tuple[str, ...]
    violations: tuple[Violation, ...]
    uncovered: tuple[str, ...]
    undocumented_commits: tuple[tuple[str, str], ...]
    # Stale file→doc pairs that didn't recur across enough commits to fail the build. Counted,
    # not listed: on a repo where specky was just installed this is most of them.
    weak_links: int = 0

    @property
    def covered(self) -> int:
        return len(self.code_files) - len(self.uncovered)

    @property
    def coverage_percent(self) -> int:
        if not self.code_files:
            return 100
        return round(100 * self.covered / len(self.code_files))

    def as_dict(self) -> dict:
        return {
            "base": self.base,
            "code_files": list(self.code_files),
            "touched_docs": list(self.touched_docs),
            "violations": [v.as_dict() for v in self.violations],
            "uncovered": list(self.uncovered),
            "undocumented_commits": [
                {"sha": sha, "subject": s} for sha, s in self.undocumented_commits
            ],
            "weak_links": self.weak_links,
            "coverage": {
                "covered": self.covered,
                "changed": len(self.code_files),
                "percent": self.coverage_percent,
            },
        }


def _domain(doc_path: str) -> str:
    """`specs/<domain>/<topic>.md` → `<domain>`; anything shallower is its own bucket."""
    parts = doc_path.split("/")
    return parts[1] if len(parts) > 2 else doc_path


def _documented(pairs: list[tuple[str, int]], touched_docs: list[str]) -> bool:
    """Did this range document a file's change anywhere that covers that file?

    Two deliberate relaxations, both aimed at the same thing — a gate that asks for one honest doc
    update rather than a checklist:

    - *Any* covering doc counts, not all of them. A file described by several docs gets changed for
      one reason at a time; demanding an edit to every doc that mentions it is how a gate teaches
      people to write filler.
    - A sibling doc in the same **domain** counts too. A domain is specky's unit of functional
      grouping, and a change most often documents itself by adding a *new* doc, which can't be in
      `doc_files` yet because nothing has linked it to a commit.
    """
    domains = {_domain(d) for d in touched_docs}
    return any(doc in touched_docs or _domain(doc) in domains for doc, _ in pairs)


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout


def resolve_base(repo_root: Path, base: str | None = None, since: str | None = None) -> str:
    """The revision this range starts from.

    `--base` wins. Otherwise `--since` picks it: a revision is used as-is, and a date
    ("2 weeks ago") resolves to the parent of the oldest commit in that window. With neither,
    `origin/HEAD` is the right default in CI — it's the branch a pull request targets — falling
    back to `HEAD~1` locally and to the empty tree in a repo with one commit.
    """
    if base is not None:
        if not _is_revision(repo_root, base):
            raise ValueError(f"{base} isn't a revision in this repo")
        return base
    if since is not None:
        if _is_revision(repo_root, since):
            return since
        shas = _git(repo_root, "log", "--format=%H", f"--since={since}", "HEAD").split()
        if not shas:
            return "HEAD"  # nothing landed in that window, so the range is empty
        oldest = shas[-1]  # --format order is newest-first, so the window starts at the end
        return f"{oldest}^" if _is_revision(repo_root, f"{oldest}^") else EMPTY_TREE
    for candidate in ("origin/HEAD", "HEAD~1"):
        if _is_revision(repo_root, candidate):
            return candidate
    return EMPTY_TREE


def _changed_files(repo_root: Path, base: str) -> list[str]:
    # Three dots: compare against the merge base, so a pull request isn't blamed for files the
    # target branch changed after it forked.
    spec = f"{base}...HEAD" if base not in (EMPTY_TREE, "HEAD") else f"{base}..HEAD"
    return [f for f in _git(repo_root, "diff", "--name-only", spec).splitlines() if f]


def _undocumented_commits(repo_root: Path, base: str) -> list[tuple[str, str]]:
    history_dir = repo_root / "specs" / "history"
    # The empty tree isn't a commit, so there's no range to exclude — that case is "all of it".
    revs = "HEAD" if base == EMPTY_TREE else f"{base}..HEAD"
    log = _git(repo_root, "log", "--reverse", "--format=%H%x1f%s", revs)
    pending = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\x1f")
        if subject.startswith(_AUTO_COMMIT_MARKER) or history_doc_for(history_dir, sha):
            continue
        pending.append((sha, subject))
    return pending


def _covering_docs(repo_root: Path, paths: list[str]) -> dict[str, list[tuple[str, int]]]:
    """`{file: [(doc_path, how many linked commits paired them), ...]}` from the precomputed
    table. Joined against `documents` so a doc that has since been deleted can't be demanded."""
    if not paths:
        return {}
    conn = connect(repo_root)
    try:
        placeholders = ",".join("?" * len(paths))
        rows = conn.execute(
            f"SELECT doc_files.path, doc_files.doc_path, doc_files.commits FROM doc_files "
            f"JOIN documents ON documents.path = doc_files.doc_path "
            f"WHERE doc_files.path IN ({placeholders}) "
            f"ORDER BY doc_files.path, doc_files.commits DESC, doc_files.doc_path",
            paths,
        ).fetchall()
    finally:
        conn.close()
    covering: dict[str, list[tuple[str, int]]] = {}
    for path, doc_path, commits in rows:
        covering.setdefault(path, []).append((doc_path, commits))
    return covering


def run_check(repo_root: Path, base: str | None = None, since: str | None = None) -> Report:
    config = CheckConfig.load(repo_root)
    resolved = resolve_base(repo_root, base=base, since=since)
    changed = _changed_files(repo_root, resolved)

    touched_docs = sorted(f for f in changed if f.startswith("specs/"))
    code_files = sorted(f for f in changed if not f.startswith("specs/") and not config.ignores(f))

    covering = _covering_docs(repo_root, code_files)
    stale_docs = [
        (path, doc_path, commits)
        for path in code_files
        if (pairs := covering.get(path)) and not _documented(pairs, touched_docs)
        for doc_path, commits in pairs
    ]
    return Report(
        base=resolved,
        code_files=tuple(code_files),
        touched_docs=tuple(touched_docs),
        violations=tuple(
            Violation(path, doc_path)
            for path, doc_path, commits in stale_docs
            if commits >= config.min_link_commits
        ),
        weak_links=sum(1 for _, _, commits in stale_docs if commits < config.min_link_commits),
        uncovered=tuple(f for f in code_files if f not in covering),
        undocumented_commits=tuple(_undocumented_commits(repo_root, resolved)),
    )


def report_lines(report: Report) -> list[str]:
    lines = [
        f"specky check: {len(report.code_files)} code file(s) changed since {report.base}, "
        f"{len(report.touched_docs)} doc(s) updated"
    ]
    if report.violations:
        lines.append("")
        lines.append("Docs describing changed code that this range didn't update:")
        lines += [f"  {v.path} → {v.doc_path}" for v in report.violations]
    if report.undocumented_commits:
        lines.append("")
        lines.append(
            f"Warning: {len(report.undocumented_commits)} commit(s) have no specs/history/ doc "
            "— the post-commit hook may not be installed (`specky install-git-hook`, then "
            "`specky sync`):"
        )
        lines += [f"  {sha[:8]} {subject}" for sha, subject in report.undocumented_commits]
    if report.uncovered:
        lines.append("")
        lines.append(f"Warning: {len(report.uncovered)} changed file(s) have no doc at all:")
        lines += [f"  {path}" for path in report.uncovered]
    if report.weak_links:
        lines.append("")
        lines.append(
            f"Note: {report.weak_links} file/doc link(s) seen in only one commit weren't enforced "
            f"— raise or lower [check] min_link_commits to change that"
        )
    lines.append("")
    lines.append(
        f"Coverage: {report.covered}/{len(report.code_files)} changed files described by a doc "
        f"({report.coverage_percent}%)"
    )
    return lines
