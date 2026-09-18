"""`specky check` — the CI gate: did this change's code touch a doc that describes it?

No AI provider is ever called and no history is walked, so this is free and fast enough to run
on every pull request. It answers from two things that already exist: the diff for the range,
and the `doc_files` table `specky index` derives from git log (see indexer.index_doc_files). A
violation is a code file whose covering doc no commit in the range updated.

Fails by default — a doc gate that only warns is a doc gate nobody notices — with `--advisory`
to print the identical report and exit 0, which is how a repo adopts this before it's clean.
Everything else it reports (commits with no history doc, changed files with no doc at all, docs
that had already fallen behind their code per staleness.py, docs with no `owner:` to ask, the
coverage figure) is advice and never affects the exit code: those are states a repo grows into,
not regressions a contributor introduced.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from specky import paths
from specky.commit_doc import _AUTO_COMMIT_MARKER, _is_revision, history_doc_for
from specky.db import connect
from specky.paths import read_table as _read_table
from specky.staleness import days_behind

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


# How far a doc may lag the code it covers before staleness.py calls it stale. Two weeks, because
# specky's own hook documents a commit within seconds of it landing: a doc that's a fortnight
# behind wasn't written by the hook and wasn't updated by hand either.
STALE_AFTER_DAYS = 14

# How many docs each advice section (stale, unowned) lists before summarising the rest. A 300-file
# pull request can pull in hundreds of covering docs, and a wall of advice buries the violations
# above it. The JSON output carries all of them.
STALE_LIST_LIMIT = 10


@dataclass(frozen=True)
class CheckConfig:
    ignore: tuple[str, ...] = DEFAULT_IGNORE
    min_link_commits: int = MIN_LINK_COMMITS
    stale_after_days: int = STALE_AFTER_DAYS

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
            stale_after_days=max(0, int(table.get("stale_after_days", STALE_AFTER_DAYS))),
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
class StaleDoc:
    doc_path: str
    days: int

    def as_dict(self) -> dict:
        return {"doc_path": self.doc_path, "days": self.days}


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
    # Docs covering this range's files that had already fallen behind their code before this
    # change (staleness.py's verdict, stored at index time). Advice, never a failure.
    stale: tuple[StaleDoc, ...] = ()
    # Stale docs elsewhere in the repo. A count, because listing every one of them would bury the
    # part of the report that's about the range in front of you.
    stale_elsewhere: int = 0
    # Classified docs in play for this range that carry no `owner:`, so a reader who lands on them
    # has nobody to ask. Advice, never a failure — an owner is a fact about the team, and no diff
    # can be said to have broken it.
    unowned: tuple[str, ...] = ()
    # Workflow docs in play that aren't the shape a workflow doc is meant to be (see
    # `generator.WORKFLOW_STYLE_INSTRUCTIONS`), as `(doc path, what's missing)`. Advice for the
    # same reason `unowned` is: the doc predates the template more often than a diff broke it.
    misshapen: tuple[tuple[str, str], ...] = ()

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
            "stale": [s.as_dict() for s in self.stale],
            "stale_elsewhere": self.stale_elsewhere,
            "unowned": list(self.unowned),
            "misshapen": [{"doc_path": path, "missing": missing} for path, missing in self.misshapen],
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
    # `errors="replace"`: these read diffs and paths, i.e. bytes specky didn't write. A CI gate
    # must not fail on a non-UTF-8 file in the range (see _commit_info in commit_doc.py).
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, errors="replace", check=True
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
    history_dir = paths.history_dir(repo_root)
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


def _covering_docs(repo_root: Path, files: list[str]) -> dict[str, list[tuple[str, int]]]:
    """`{file: [(doc_path, how many linked commits paired them), ...]}` from the precomputed
    table. Joined against `documents` so a doc that has since been deleted can't be demanded."""
    if not files:
        return {}
    conn = connect(repo_root)
    try:
        placeholders = ",".join("?" * len(files))
        rows = conn.execute(
            f"SELECT doc_files.path, doc_files.doc_path, doc_files.commits FROM doc_files "
            f"JOIN documents ON documents.path = doc_files.doc_path "
            f"WHERE doc_files.path IN ({placeholders}) "
            f"ORDER BY doc_files.path, doc_files.commits DESC, doc_files.doc_path",
            files,
        ).fetchall()
    finally:
        conn.close()
    covering: dict[str, list[tuple[str, int]]] = {}
    for path, doc_path, commits in rows:
        covering.setdefault(path, []).append((doc_path, commits))
    return covering


def _stale_docs(repo_root: Path) -> dict[str, int]:
    """`{doc path: days its code ran ahead of it}` for every doc staleness.py flagged.

    Read from the index rather than recomputed, so this costs one query and agrees with the badge
    the HTML viewer shows. An index built before staleness existed has no rows here, which reads
    as "nothing is stale" — the honest answer, since nothing was measured.
    """
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, stale_since, last_code_change FROM documents WHERE stale_since != ''"
        ).fetchall()
    finally:
        conn.close()
    return {path: days_behind(since, code) for path, since, code in rows}


def _unowned_docs(repo_root: Path) -> set[str]:
    """Classified docs with an empty `owner:`.

    Only classified ones (`doc_type != ''`): `specs/history/` holds one doc per commit, whose owner
    is the commit's author and which nobody is meant to hand-edit, so asking those to name an owner
    would be thousands of lines of advice about docs that don't want it.
    """
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path FROM documents WHERE doc_type != '' AND owner = ''"
        ).fetchall()
    finally:
        conn.close()
    return {path for (path,) in rows}


def _misshapen_workflows(repo_root: Path, docs: set[str]) -> dict[str, str]:
    """`{doc path: what it's missing}` for each of `docs` that is a workflow doc and isn't the shape
    the template asks for — a diagram of its happy path, and one place gathering the branches off it.

    Read from the indexed content rather than the worktree, like `_stale_docs`, so this says the same
    thing the viewer is showing. Unlike `_stale_docs` and `_unowned_docs` it is scoped in SQL rather
    than after the fact, because it is the only one of the three that reads `content`: fetching every
    workflow doc's full body to report on the handful in this range is the whole corpus in memory for
    nothing. Only `doc_type = 'workflow'`: a feature doc earns a diagram rather than owing one, and
    has no Edge Cases section to be missing.
    """
    if not docs:
        return {}
    conn = connect(repo_root)
    try:
        placeholders = ",".join("?" * len(docs))
        rows = conn.execute(
            "SELECT path, content FROM documents "
            f"WHERE doc_type = 'workflow' AND path IN ({placeholders})",
            sorted(docs),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for path, content in rows:
        lowered = content.lower()
        missing = []
        if "```mermaid" not in lowered:
            missing.append("no diagram")
        if "\n## edge cases" not in lowered:
            missing.append("no `## Edge Cases` section")
        if missing:
            out[path] = " and ".join(missing)
    return out


def run_check(repo_root: Path, base: str | None = None, since: str | None = None) -> Report:
    config = CheckConfig.load(repo_root)
    resolved = resolve_base(repo_root, base=base, since=since)
    changed = _changed_files(repo_root, resolved)

    docs_prefix = paths.docs_prefix(repo_root)
    touched_docs = sorted(f for f in changed if f.startswith(docs_prefix))
    code_files = sorted(
        f for f in changed if not f.startswith(docs_prefix) and not config.ignores(f)
    )

    covering = _covering_docs(repo_root, code_files)
    undocumented = [
        (path, doc_path, commits)
        for path in code_files
        if (pairs := covering.get(path)) and not _documented(pairs, touched_docs)
        for doc_path, commits in pairs
    ]

    # Staleness is about this range only in as much as it covers the same files: a doc that was
    # already 40 days behind is worth saying while someone is looking at that code, and the rest of
    # the repo's stale docs are a number rather than a list. A doc this range updated isn't
    # reported, whatever the index still says — the update is the fix.
    stale = _stale_docs(repo_root)
    in_range = {doc for path in code_files for doc, _ in covering.get(path, ())}
    covers_range = [d for d in stale if d in in_range]
    relevant = sorted(
        (d for d in covers_range if d not in touched_docs), key=lambda d: (-stale[d], d)
    )
    # Scoped the same way, plus the docs this range edited: someone with the doc already open is
    # exactly who can add the missing line, and the rest of the repo's unowned docs aren't this
    # pull request's business.
    unowned = _unowned_docs(repo_root)
    # Scoped exactly like `unowned`, and for the same reason: a pull request shouldn't be handed a
    # list of workflow docs it never opened.
    in_play = in_range | set(touched_docs)
    misshapen = _misshapen_workflows(repo_root, in_play)
    return Report(
        base=resolved,
        code_files=tuple(code_files),
        touched_docs=tuple(touched_docs),
        violations=tuple(
            Violation(path, doc_path)
            for path, doc_path, commits in undocumented
            if commits >= config.min_link_commits
        ),
        weak_links=sum(1 for _, _, commits in undocumented if commits < config.min_link_commits),
        stale=tuple(StaleDoc(doc, stale[doc]) for doc in relevant),
        stale_elsewhere=len(stale) - len(covers_range),
        unowned=tuple(sorted(unowned & in_play)),
        misshapen=tuple(sorted(misshapen.items())),
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
            f"Warning: {len(report.undocumented_commits)} commit(s) have no history doc "
            "— the git hooks may not be installed (`specky install-git-hook`, then "
            "`specky sync`):"
        )
        lines += [f"  {sha[:8]} {subject}" for sha, subject in report.undocumented_commits]
    if report.uncovered:
        lines.append("")
        lines.append(f"Warning: {len(report.uncovered)} changed file(s) have no doc at all:")
        lines += [f"  {path}" for path in report.uncovered]
    if report.stale:
        lines.append("")
        lines.append(
            f"Warning: {len(report.stale)} doc(s) covering this range's code had already fallen "
            "behind it:"
        )
        shown = report.stale[:STALE_LIST_LIMIT]
        lines += [f"  {s.doc_path} — {s.days} days behind" for s in shown]
        if len(report.stale) > STALE_LIST_LIMIT:
            lines.append(f"  … and {len(report.stale) - STALE_LIST_LIMIT} more")
    if report.unowned:
        lines.append("")
        lines.append(
            f"Note: {len(report.unowned)} doc(s) in this range have no `owner:` — add one to the "
            "frontmatter and the viewer shows a 'Who to ask' line:"
        )
        lines += [f"  {path}" for path in report.unowned[:STALE_LIST_LIMIT]]
        if len(report.unowned) > STALE_LIST_LIMIT:
            lines.append(f"  … and {len(report.unowned) - STALE_LIST_LIMIT} more")
    if report.misshapen:
        lines.append("")
        lines.append(
            f"Note: {len(report.misshapen)} workflow doc(s) in this range aren't the shape a "
            "workflow doc is meant to be — the happy path under `## How It Works`, a ```mermaid``` "
            "diagram of it directly below, and the branches off it in `## Edge Cases`:"
        )
        lines += [
            f"  {path} — {missing}" for path, missing in report.misshapen[:STALE_LIST_LIMIT]
        ]
        if len(report.misshapen) > STALE_LIST_LIMIT:
            lines.append(f"  … and {len(report.misshapen) - STALE_LIST_LIMIT} more")
    if report.stale_elsewhere:
        lines.append("")
        lines.append(
            f"Note: {report.stale_elsewhere} doc(s) elsewhere in the docs tree are behind their "
            "code too "
            "— the viewer's Stale filter lists them"
        )
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
