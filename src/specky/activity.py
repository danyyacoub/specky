"""Who changed what on the mainline recently — the home page's activity brief, for a reader who
wants the shape of the work rather than its commits. Git and the committed history docs only: no
AI call, and a fixed number of git processes however long the history is.

**What counts as one change.** The walk is `git log --first-parent <mainline>`, because that is
the only view of a history in which a pull request is one thing. A merge commit is one change for
its whole branch: the commits it brought in (`M^1..M^2`) are folded into it — they are its people
and its words, never entries of their own — so "wip", "address review" and "fix typo" disappear
into the PR they were part of. A commit with one parent (a direct push, or a squash) is a change on
its own. Commits the mainline can't reach yet are work in progress, grouped by the branch they're
on.

**Where the words come from.** The history docs (`<docs root>/history/`), read with
`commit_doc.read_history`: from the working tree when the doc is there — the same file the viewer
renders as that commit's page, so the brief and the page it links to never disagree — and
otherwise from the tree of the branch the commit is on, which is how an unmerged branch's docs,
committed only on that branch, are read at all. A merge's own doc is never used: `git show` of a clean
merge is empty, so specky no longer writes one (see `commit_doc.pending_commits`), and an old one
says nothing its branch's docs don't. A commit with no doc falls back to its subject, marked, and
counted so the reader can see how much of the picture is missing. The commits one history entry
covers share its words, so they share one line.

**People, not agents.** Authors and `Co-authored-by` trailers, minus bots and coding agents
(`is_agent`), with specky's own doc-sync commits dropped outright. A change that no human touched
is counted, not shown.

**Cost.** About a dozen git processes, and a fixed number of them, none proportional to the
history: the first-parent window, one walk of every commit it introduced, one `--name-only` walk
for the files, one walk of the unmerged branches, one `cat-file --batch` for every legacy history
doc at once, one `log` plus one `cat-file --batch` for the entries, and a few `rev-parse`/
`for-each-ref`/`remote get-url` lookups. The grouping of a merge's commits is a walk over the
parent map in Python rather than a `git log M^1..M^2` per merge.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from specky import gitlog, paths
from specky.check import CheckConfig, covering_docs
from specky.commit_doc import (
    _AUTO_COMMIT_MARKER,
    HistoryIndex,
    MicroDoc,
    _is_revision,
    brief,
    history_names,
    is_legacy_name,
    read_history,
)

DEFAULT_DAYS = 14

# Tried in order when `[activity] branch` isn't set. `dev`/`develop` come first on purpose: in a
# gitflow repo work lands there, and `main` only ever receives release merges — whose first-parent
# view is one "Merge release/1.4" per fortnight with everyone's name on it.
MAINLINE_CANDIDATES = ("origin/dev", "origin/develop", "origin/HEAD", "HEAD")

# Long-lived branches, never shown as work in progress: their commits aren't a branch someone is
# working on, and in gitflow `main` carries release merges and hotfixes that `dev` hasn't
# back-merged yet.
LONG_LIVED = frozenset({"HEAD", "main", "master", "dev", "develop", "trunk"})

# Coding agents and bots, recognised by address rather than by name: "Claude" and "Devin" are also
# people's first names, and an address is what these tools actually commit under. `[bot]` covers
# GitHub Apps (dependabot, renovate, github-actions, claude, devin-ai-integration, gemini-code-assist,
# chatgpt-codex-connector); the rest are the agents that commit or co-author as themselves.
# `noreply@github.com` is GitHub's own web-flow identity, which merges and web edits are committed as.
_AGENT_EMAILS = frozenset(
    {"noreply@anthropic.com", "noreply@github.com", "cursoragent@cursor.com"}
)
_AGENT_EMAIL_SUFFIXES = ("+copilot@users.noreply.github.com",)

_FIELDS = (
    "%H%x1f%P%x1f%aN%x1f%aE%x1f%cI%x1f"
    "%(trailers:key=Co-authored-by,valueonly,separator=%x1e)%x1f%s%x1f%S%x1f%b"
)
_IDENTITY = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$")

# What a merge commit's message says about the pull request behind it, per forge. The body line is
# the PR's title where the forge puts one there (GitHub, and GitLab's default template).
_GITHUB_MERGE = re.compile(r"^Merge pull request #(?P<pr>\d+) from (?P<branch>\S+)")
_GITLAB_MERGE = re.compile(r"^Merge branch '(?P<branch>[^']+)'(?: into '[^']+')?")
_BITBUCKET_MERGE = re.compile(r"^Merged in (?P<branch>\S+)(?: \(pull request #(?P<pr>\d+)\))?")
_GITLAB_MR = re.compile(r"See merge request \S*!(?P<pr>\d+)")
_SQUASH_PR = re.compile(r"\(#(?P<pr>\d+)\)\s*$")
_BRANCH_TYPE = re.compile(
    r"^(?:feat|feature|fix|bugfix|hotfix|chore|refactor|docs|perf|test|build|ci)[/-]", re.I
)
_REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?[:/](?P<path>.+?)(?:\.git)?/?$")

# `IN (?, ?, …)` placeholders per coverage query, under SQLite's oldest variable limit.
_COVERAGE_CHUNK = 500


@dataclass(frozen=True)
class ActivityConfig:
    branch: str = ""
    days: int = DEFAULT_DAYS
    ignore_authors: tuple[str, ...] = ()
    enabled: bool = True

    @classmethod
    def load(cls, repo_root: Path) -> ActivityConfig:
        """`[activity]` in specky.toml, or `[tool.specky.activity]` in pyproject.toml — the same
        two spellings, for the same reason, as `CheckConfig.load`."""
        table = paths.read_table(repo_root / "specky.toml", ("activity",)) or paths.read_table(
            repo_root / "pyproject.toml", ("tool", "specky", "activity")
        )
        ignore = table.get("ignore_authors", ())
        return cls(
            branch=str(table.get("branch", "")).strip(),
            days=max(1, int(table.get("days", DEFAULT_DAYS))),
            ignore_authors=tuple([ignore] if isinstance(ignore, str) else ignore),
            enabled=bool(table.get("enabled", True)),
        )


@dataclass(frozen=True)
class Line:
    """One sentence of the brief: what one commit did."""

    text: str
    detail: str  # the doc's what/why prose, for a hover; "" without a doc
    impact: str
    history_path: str  # the doc it came from, repo-relative; "" when the commit has none


@dataclass
class Change:
    """One first-parent commit on the mainline: a merged branch, or a commit on its own."""

    sha: str
    date: datetime  # when it landed: the mainline commit's committer date
    lines: list[Line]
    label: str  # the PR title or branch, for a merge; empty for a commit that is its own line
    pr_url: str
    commits: int  # how many commits it folds in
    people: list[str]
    features: list[str]  # doc paths, most direct evidence first


@dataclass
class Branch:
    ref: str
    date: datetime  # its newest commit in the window
    lines: list[Line]
    people: list[str]
    features: list[str]
    commits: int = 0  # its commits in the window; several can share one line (one entry)


@dataclass
class Person:
    name: str
    shipped: list[Change] = field(default_factory=list)
    in_progress: list[Branch] = field(default_factory=list)

    @property
    def last_active(self) -> datetime:
        return max([c.date for c in self.shipped] + [b.date for b in self.in_progress])

    def features(self) -> list[str]:
        """Their docs, most often touched first."""
        counts = Counter(f for item in [*self.shipped, *self.in_progress] for f in item.features)
        return [doc for doc, _ in counts.most_common()]


@dataclass
class Activity:
    branch: str
    days: int
    as_of: datetime
    people: list[Person]
    automated: int = 0  # changes no human touched, left out
    undocumented: int = 0  # commits shown with their subject because they have no history doc
    shallow: bool = False


@dataclass
class _Commit:
    sha: str
    parents: list[str]
    author: tuple[str, str]
    date: datetime
    coauthors: list[tuple[str, str]]
    subject: str
    source: str
    body: str


def is_agent(name: str, email: str, ignore: tuple[str, ...] = ()) -> bool:
    email = email.strip().lower()
    if "[bot]" in name.lower() or "[bot]" in email:
        return True
    if email in _AGENT_EMAILS or email.endswith(_AGENT_EMAIL_SUFFIXES):
        return True
    identity = f"{name.strip()} <{email}>".lower()
    return any(fnmatch(identity, pattern.lower()) for pattern in ignore)


def mainline(repo_root: Path, configured: str = "") -> str:
    """The branch whose first-parent history is "what landed". Raises ValueError for a configured
    one that isn't a revision here, rather than silently showing some other branch's history."""
    if configured:
        if not _is_revision(repo_root, configured):
            raise ValueError(f"[activity] branch {configured!r} isn't a revision in this repo")
        return configured
    for candidate in MAINLINE_CANDIDATES:
        if _is_revision(repo_root, candidate):
            return candidate
    raise ValueError("no commits yet")


def _display_name(repo_root: Path, ref: str) -> str:
    """`origin/HEAD` → `origin/main`: the header names the branch, not the pointer to it."""
    try:
        name = gitlog.run(repo_root, ["rev-parse", "--abbrev-ref", ref]).strip()
    except subprocess.CalledProcessError:
        return ref
    return name if name and name != "HEAD" else ref


def _identity(text: str) -> tuple[str, str] | None:
    match = _IDENTITY.match(text.strip())
    return (match["name"], match["email"]) if match else None


def _log(repo_root: Path, args: list[str]) -> list[_Commit]:
    """Newest first in `--topo-order`: no commit before its children. Callers wanting oldest first
    reverse the list rather than sorting by date — rebased commits share a committer timestamp, so
    a date sort would put a branch's commits in no particular order."""
    out = gitlog.run(repo_root, ["log", "--topo-order", f"--format=%x01{_FIELDS}", *args])
    commits = []
    for record in out.split(gitlog.RECORD):
        fields = record.split("\x1f", 8)
        if len(fields) < 9:
            continue
        sha, parents, name, email, date, trailers, subject, source, body = fields
        commits.append(
            _Commit(
                sha=sha,
                parents=parents.split(),
                author=(name, email),
                date=datetime.fromisoformat(date),
                coauthors=[i for t in trailers.split("\x1e") if t and (i := _identity(t))],
                subject=subject,
                source=source,
                body=body.strip(),
            )
        )
    return commits


def _read_blobs(repo_root: Path, specs: list[str]) -> list[str | None]:
    """`git cat-file --batch` over `<tree-ish>:<path>` specs, in order; None for a missing one.

    Bytes, not text: the batch format gives each blob's size in bytes, and a history doc is prose
    that can hold any character a model wrote.
    """
    if not specs:
        return []
    out = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo_root,
        input=("\n".join(specs) + "\n").encode(),
        capture_output=True,
        check=True,
    ).stdout
    blobs: list[str | None] = []
    pos = 0
    for _ in specs:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode(errors="replace").split()
        pos = end + 1
        if len(header) != 3 or header[1] != "blob":  # `<spec> missing` / `ambiguous`
            blobs.append(None)
            continue
        size = int(header[2])
        blobs.append(out[pos : pos + size].decode(errors="replace"))
        pos += size + 1
    return blobs


def _history_docs(
    repo_root: Path, wanted: list[tuple[str, str]], since: str
) -> dict[str, tuple[str, MicroDoc]]:
    """`{sha: (doc path, what it says)}` for `(tree-ish, sha)` pairs — the working tree's doc where
    there is one, else the doc in that tree-ish.

    A legacy doc is named for its commit: both possible names are read from the tree-ish, with
    `commit_doc.history_doc_for`'s rule for which one is this commit's — the `sha:` it records, or
    none recorded at all. An entry is named for its branch or its subject, so no name can be worked
    out from a sha: the entries are found instead, as every history file the window's commits on
    those tree-ishes touched, read at the newest commit that touched it and mapped to the commits
    its `commits:` lists. One `git log` and one more `cat-file --batch`, however many there are.
    """
    found: dict[str, tuple[str, MicroDoc]] = {}
    history = HistoryIndex(paths.history_dir(repo_root))
    for _, sha in wanted:
        local = history.doc_for(sha)
        if local is not None and (parsed := read_history(local.read_text())) is not None:
            found[sha] = (str(local.relative_to(repo_root)), parsed[1])
    wanted = [(tree, sha) for tree, sha in wanted if sha not in found]
    if not wanted:
        return found

    prefix = paths.history_prefix(repo_root)
    names = [tuple(prefix + name for name in history_names(sha)) for _, sha in wanted]
    specs = [f"{tree}:{name}" for (tree, _), pair in zip(wanted, names) for name in pair]
    blobs = iter(_read_blobs(repo_root, specs))
    for (_, sha), pair in zip(wanted, names):
        for name in pair:
            text = next(blobs)
            parsed = read_history(text) if text is not None else None
            if parsed is not None and parsed[0] in (None, sha) and sha not in found:
                found[sha] = (name, parsed[1])

    missing = {sha for _, sha in wanted if sha not in found}
    if not missing:
        return found
    trees = list(dict.fromkeys(tree for tree, sha in wanted if sha in missing))
    log = gitlog.run(repo_root, ["log", "--format=%x01%H", "--name-only", since, *trees, "--", prefix])
    newest: dict[str, str] = {}  # entry path -> the newest commit that touched it
    for lines in gitlog.blocks(log):
        for name in lines[1:]:
            if name and not is_legacy_name(Path(name)):
                newest.setdefault(name, lines[0])
    entries = list(newest.items())
    for (name, _), text in zip(entries, _read_blobs(repo_root, [f"{c}:{n}" for n, c in entries])):
        parsed = read_history(text) if text is not None else None
        for sha in parsed[1].commits if parsed is not None else ():
            if sha in missing and sha not in found:
                found[sha] = (name, parsed[1])
    return found


def _lines(commits: list[_Commit], docs: dict[str, tuple[str, MicroDoc]]) -> list[Line]:
    """One line per history doc: the commits of an entry share its words, so they share a line."""
    lines: list[Line] = []
    seen: set[str] = set()
    for commit in commits:
        line = _line(commit, docs)
        if line.history_path in seen:
            continue
        if line.history_path:
            seen.add(line.history_path)
        lines.append(line)
    return lines


def _line(commit: _Commit, docs: dict[str, tuple[str, MicroDoc]]) -> Line:
    if commit.sha not in docs:
        return Line(text=commit.subject, detail="", impact="", history_path="")
    path, doc = docs[commit.sha]
    return Line(
        text=brief(doc),
        detail="\n\n".join(p for p in (doc.what, doc.why) if p),
        impact=doc.impact,
        history_path=path,
    )


def _readable_branch(branch: str) -> str:
    """`octo/feat/refund-limits` → `refund limits`: a merge with no PR title still says what."""
    if "/" in branch and not _BRANCH_TYPE.match(branch):
        branch = branch.split("/", 1)[1]  # GitHub's `owner/branch`
    return _BRANCH_TYPE.sub("", branch).replace("-", " ").replace("_", " ").strip()


def _merge_label(commit: _Commit) -> tuple[str, str]:
    """`(label, PR number)` from a merge commit's message."""
    title = next((line.strip() for line in commit.body.splitlines() if line.strip()), "")
    if title.startswith("See merge request"):
        title = ""
    for pattern in (_GITHUB_MERGE, _GITLAB_MERGE, _BITBUCKET_MERGE):
        if match := pattern.match(commit.subject):
            pr = match.groupdict().get("pr") or ""
            if not pr and (mr := _GITLAB_MR.search(commit.body)):
                pr = mr["pr"]
            return title or _readable_branch(match["branch"]), pr
    return title or commit.subject, ""


def _pr_link(repo_root: Path) -> Callable[[str], str] | None:
    """`number → URL` for origin's forge, or None when it isn't one this knows."""
    try:
        url = gitlog.run(repo_root, ["remote", "get-url", "origin"]).strip()
    except subprocess.CalledProcessError:
        return None
    match = _REMOTE_URL.match(url)
    if not match:
        return None
    host, path = match["host"], match["path"]
    if "github" in host:
        return lambda n: f"https://{host}/{path}/pull/{n}"
    if "gitlab" in host:
        return lambda n: f"https://{host}/{path}/-/merge_requests/{n}"
    return None


class _People:
    """Names for identities, merged where the name matches: one person committing from a work and
    a personal address shows up once. `.mailmap` (which `%aN`/`%aE` honour) is the override."""

    def __init__(self, ignore: tuple[str, ...]) -> None:
        self._ignore = ignore
        self._names: dict[str, Counter[str]] = {}

    def humans(self, identities: list[tuple[str, str]]) -> list[str]:
        """Emails of the humans among these, first-seen order, recording each name used."""
        emails: list[str] = []
        for name, email in identities:
            if is_agent(name, email, self._ignore):
                continue
            key = email.strip().lower()
            self._names.setdefault(key, Counter())[name.strip()] += 1
            if key not in emails:
                emails.append(key)
        return emails

    def name(self, email: str) -> str:
        return self._names[email].most_common(1)[0][0] or email


# Docs guessed from coverage when a change says nothing more direct about what it touched. Few, on
# purpose: coverage pairs a file with every doc that ever rode in a commit with it, so past the
# strongest couple it names docs the change had nothing to do with.
COVERAGE_GUESSES = 2


def _features(
    repo_root: Path, stated: list[str], files: list[str], covering: dict, min_commits: int
) -> list[str]:
    """The docs a change touched, strongest evidence first: what its history docs say
    (`features:`), then the feature docs it edited — and only when neither says anything, the docs
    `doc_files` says cover its code, ranked by how many commits link them."""
    docs_prefix, history_prefix = paths.docs_prefix(repo_root), paths.history_prefix(repo_root)
    edited = [
        f
        for f in files
        if f.startswith(docs_prefix)
        and not f.startswith(history_prefix)
        and f.count("/") >= 2
        and f.endswith(".md")
    ]
    if stated or edited:
        return _dedupe(stated + edited)
    weight: Counter[str] = Counter()
    for f in files:
        for doc, n in covering.get(f, ()):
            if n >= min_commits:
                weight[doc] += n
    return [doc for doc, _ in weight.most_common(COVERAGE_GUESSES)]


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _identities(commits: list[_Commit]) -> list[tuple[str, str]]:
    return [identity for c in commits for identity in (c.author, *c.coauthors)]


def _stated_features(commits: list[_Commit], docs: dict[str, tuple[str, MicroDoc]]) -> list[str]:
    """What the commits' history docs say they touched (`features:`), in commit order."""
    return [f for c in commits if c.sha in docs for f in docs[c.sha][1].features]


def _walk_mainline(
    repo_root: Path, tip: str, since: str
) -> tuple[list[tuple[_Commit, list[_Commit]]], dict[str, list[str]]]:
    """The window's changes, oldest first — `(first-parent commit, the work it brought in)` — and
    the files each first-parent commit changed. specky's doc-sync commits are not work."""
    spine = [
        line.split("\x1f")
        for line in gitlog.run(
            repo_root, ["log", "--first-parent", since, "--format=%H%x1f%P", tip]
        ).splitlines()
        if line
    ]
    if not spine:
        return [], {}
    # Everything the window's first-parent commits introduced, down to the parent of the oldest —
    # `^boundary` rather than `--since`, so a branch commit written before the window but merged
    # inside it is still that merge's.
    walk = [tip, *(f"^{b}" for b in spine[-1][1].split()[:1])]
    introduced = {c.sha: c for c in _log(repo_root, walk)}
    files_log = gitlog.run(
        repo_root,
        ["log", "--first-parent", "--diff-merges=first-parent", "--name-only", "--format=%x01%H", *walk],
    )
    files = {lines[0]: [f for f in lines[1:] if f] for lines in gitlog.blocks(files_log)}
    groups = []
    for head, brought_in in _group_merges([sha for sha, _ in spine], introduced):
        work = [c for c in brought_in if not c.subject.startswith(_AUTO_COMMIT_MARKER)]
        if work:
            groups.append((head, work))
    return groups, files


def _group_merges(
    spine: list[str], introduced: dict[str, _Commit]
) -> list[tuple[_Commit, list[_Commit]]]:
    """Each first-parent commit with the commits it brought in, oldest first: a merge gets the
    commits reachable from its other parents that nothing earlier claimed, a commit with one parent
    gets itself.

    Oldest merge first, so a commit two merges could both reach belongs to the earlier one — which
    is what `M^1..M^2` would say, since the later merge's first parent already has it. A walk over
    the parent map rather than a `git log M^1..M^2` per merge.
    """
    order = {sha: i for i, sha in enumerate(introduced)}  # topo position: larger is older
    on_spine = set(spine)
    claimed: set[str] = set()
    groups: list[tuple[_Commit, list[_Commit]]] = []
    for sha in reversed(spine):
        head = introduced.get(sha)
        if head is None:
            continue
        if len(head.parents) < 2:
            groups.append((head, [head]))
            continue
        folded, stack = [], list(head.parents[1:])
        while stack:
            s = stack.pop()
            if s in claimed or s in on_spine or s not in introduced:
                continue
            claimed.add(s)
            folded.append(introduced[s])
            stack.extend(introduced[s].parents)
        groups.append((head, sorted(folded, key=lambda c: order[c.sha], reverse=True)))
    return groups


def _unmerged_branches(
    repo_root: Path, tip: str, since: str, mainline_name: str
) -> dict[str, list[_Commit]]:
    """`{branch: its commits in the window, oldest first}` for work the mainline can't reach yet.

    Remote branches when the repo has a remote — what the team has pushed — and local ones when it
    doesn't. `--source` names the ref each commit was reached from, which groups them by branch in
    one process.
    """
    remote = bool(
        gitlog.run(
            repo_root, ["for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes"]
        ).strip()
    )

    def short(ref: str) -> str:  # `origin/feat/x` → `feat/x`, to compare with LONG_LIVED
        return ref.split("/", 1)[-1] if remote else ref

    branches: dict[str, list[_Commit]] = {}
    log = _log(
        repo_root,
        ["--source", "--no-merges", since, "--remotes" if remote else "--branches", f"^{tip}"],
    )
    for c in reversed(log):  # `_log` is newest first
        ref = c.source.removeprefix("refs/remotes/").removeprefix("refs/heads/")
        if c.subject.startswith(_AUTO_COMMIT_MARKER) or short(ref) in LONG_LIVED:
            continue
        if short(ref) != short(mainline_name):
            branches.setdefault(ref, []).append(c)
    return branches


def _coverage(repo_root: Path, files: dict[str, list[str]]) -> dict[str, list[tuple[str, int]]]:
    """`check.covering_docs` for every file the window touched, in bounded queries."""
    code_files = sorted({f for fs in files.values() for f in fs})
    covering: dict[str, list[tuple[str, int]]] = {}
    for start in range(0, len(code_files), _COVERAGE_CHUNK):
        covering.update(covering_docs(repo_root, code_files[start : start + _COVERAGE_CHUNK]))
    return covering


def _change_label(head: _Commit) -> tuple[str, str]:
    """`(label, PR number)`: a merge's from its message, a squash's `(#N)` alone."""
    if len(head.parents) > 1:
        label, pr = _merge_label(head)
    else:
        match = _SQUASH_PR.search(head.subject)
        label, pr = "", match["pr"] if match else ""
    return label or (f"#{pr}" if pr else ""), pr


def _by_person(items: list[Change | Branch], people: _People) -> list[Person]:
    """Emails to names, then one Person per name — so two addresses under one name are one person
    — sorted by who was active most recently."""
    merged: dict[str, Person] = {}
    for item in items:
        item.people = _dedupe([people.name(email) for email in item.people])
        for name in item.people:
            person = merged.setdefault(name.casefold(), Person(name=name))
            (person.shipped if isinstance(item, Change) else person.in_progress).append(item)
    for person in merged.values():
        # Behaviour first: a change whose every line is `internal` sinks below the rest.
        person.shipped.sort(key=lambda c: c.date, reverse=True)
        person.shipped.sort(key=lambda c: all(line.impact == "internal" for line in c.lines))
        person.in_progress.sort(key=lambda b: b.date, reverse=True)
    return sorted(merged.values(), key=lambda p: p.last_active, reverse=True)


def collect(
    repo_root: Path, cfg: ActivityConfig | None = None, now: datetime | None = None
) -> Activity | None:
    """The brief for `[activity] days` up to `now`, or None when `[activity] enabled = false`.

    Raises ValueError for a configured mainline that doesn't exist — the renderer reports it and
    leaves the section out, rather than showing the wrong branch under the right name.
    """
    cfg = cfg or ActivityConfig.load(repo_root)
    if not cfg.enabled:
        return None
    now = now or datetime.now(timezone.utc)
    ref = mainline(repo_root, cfg.branch)
    activity = Activity(branch=_display_name(repo_root, ref), days=cfg.days, as_of=now, people=[])
    if gitlog.run(repo_root, ["rev-parse", "--is-shallow-repository"]).strip() == "true":
        activity.shallow = True  # a depth-1 CI checkout: any brief from it would be wrong, not short
        return activity

    since = f"--since={(now - timedelta(days=cfg.days)).isoformat()}"
    tip = gitlog.run(repo_root, ["rev-parse", ref]).strip()
    groups, files = _walk_mainline(repo_root, tip, since)
    branches = _unmerged_branches(repo_root, tip, since, activity.branch)
    docs = _history_docs(
        repo_root,
        [(tip, c.sha) for _, work in groups for c in work]
        + [(branch, c.sha) for branch, commits in branches.items() for c in commits],
        since,
    )
    covering = _coverage(repo_root, files)
    min_commits = CheckConfig.load(repo_root).min_link_commits
    pr_link = _pr_link(repo_root)
    people = _People(cfg.ignore_authors)
    # `people` holds emails until every identity has been seen; `_by_person` settles the names.
    items: list[Change | Branch] = []

    # Direct commits on the mainline that share a history entry — one person's hotfixes, folded
    # into one entry by the hook — are one change too, told once, as of the latest of them.
    by_entry: dict[str, Change] = {}
    for head, work in groups:  # oldest first
        emails = people.humans(_identities(work))
        if not emails and len(head.parents) > 1:
            emails = people.humans([head.author])  # whoever merged a branch no human wrote
        if not emails:
            activity.automated += 1
            continue
        label, pr = _change_label(head)
        lines = _lines(work, docs)
        features = _features(
            repo_root, _stated_features(work, docs), files.get(head.sha, []), covering, min_commits
        )
        entry = lines[0].history_path if not label and len(lines) == 1 else ""
        if entry and (shared := by_entry.get(entry)) is not None:
            shared.sha, shared.date, shared.lines = head.sha, head.date, lines
            shared.commits += len(work)
            shared.people = _dedupe(shared.people + emails)
            shared.features = _dedupe(shared.features + features)
            continue
        change = Change(
            sha=head.sha,
            date=head.date,
            lines=lines,
            label=label,
            pr_url=pr_link(pr) if pr and pr_link else "",
            commits=len(work),
            people=emails,
            features=features,
        )
        if entry:
            by_entry[entry] = change
        items.append(change)

    for branch, commits in branches.items():
        emails = people.humans(_identities(commits))
        if not emails:
            activity.automated += 1
            continue
        items.append(
            Branch(
                ref=branch,
                date=max(c.date for c in commits),
                lines=_lines(commits, docs),
                people=emails,
                features=_dedupe(_stated_features(commits, docs)),
                commits=len(commits),
            )
        )

    activity.undocumented = sum(
        1 for item in items for line in item.lines if not line.history_path
    )
    activity.people = _by_person(items, people)
    return activity
