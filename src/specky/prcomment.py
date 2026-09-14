"""`specky pr-comment` — what a range did to the docs, as markdown, printed to stdout.

A reviewer looking at a pull request can see that `specs/billing/refund-flow.md` changed; what
they can't see without opening every file is *what the product now does differently*. This
prints that: the docs added, updated and removed, each with its one-line purpose from
`MODULES.md`, and for an updated doc the diff of its `## What It Does` section — the part written
for someone who doesn't read code.

It never posts anything. The output is designed to be piped:

    specky pr-comment --base origin/main | gh pr comment --body-file -

so the side-effectful step is a command the user typed, with the text in front of them first.
No AI provider is called and the index isn't read — this is git plus the docs themselves.

**Everything here is bounded**, because the range is whatever someone asks for. A branch with 300
commits has ~300 `specs/history/` docs and can touch dozens of feature docs; GitHub refuses a
comment body over 65,536 characters, and a reviewer gives up long before that. So history docs are
counted rather than listed, the lists cut off at `DOC_LIST_LIMIT`, only `DETAIL_LIMIT` docs get
their section diffed (one `git show` each, so that's also the process count), and the whole body is
trimmed to `BODY_MAX_CHARS` at a line boundary with a note saying what was cut.
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from specky import gitlog, paths
from specky.check import EMPTY_TREE, resolve_base
from specky.generator import PENDING_DIR, modules_purposes
from specky.testgen import section

# Well under GitHub's 65,536-character comment limit, leaving room for whatever a workflow wraps
# this in. The trim note says the body was cut, so an over-long range degrades visibly.
BODY_MAX_CHARS = 55_000
# Docs named in a list before the remainder becomes "… and N more".
DOC_LIST_LIMIT = 30
# Docs whose `## What It Does` is diffed. Each costs one `git show`, and past a dozen the comment
# is a document rather than a summary.
DETAIL_LIMIT = 10
# Changed lines shown per doc, and how wide one of them may be. A doc section is prose, so a single
# reworded paragraph is one very long `-`/`+` pair.
DIFF_MAX_LINES = 12
DIFF_MAX_COLS = 300
# How much of a new doc's summary is quoted.
SUMMARY_MAX_CHARS = 500

WHAT_IT_DOES = "what it does"

# Docs directly under specs/ — MODULES.md, GLOSSARY.md, PRODUCT.md. Nearly every doc change touches
# the index, and its diff is a table row rather than a statement about the product, so these are
# named on one line instead of detailed.
_INDEX_DOCS = "index docs"


def _rel(path: str) -> str:
    """`specs/billing/refund-flow.md` → `billing/refund-flow.md`.

    The docs root's own name carries no information in a comment about docs, and dropping the first
    segment rather than a literal `specs/` keeps that true in a repo that renamed it.
    """
    return path.split("/", 1)[-1]


def _kind(path: str, history_prefix: str) -> str:
    """Which part of the comment a changed doc path belongs in."""
    if path.startswith(history_prefix):
        return "history"
    return _INDEX_DOCS if path.count("/") < 2 else "doc"


@dataclass(frozen=True)
class DocChange:
    path: str
    status: str  # added | updated | removed
    purpose: str = ""
    summary: str = ""  # the new `## What It Does`, for an added doc
    diff: tuple[str, ...] = ()  # `-`/`+` lines of that section, for an updated one
    old_path: str = ""  # set when git reports a rename
    # Whether this doc's section was read at all. An updated doc with `detailed` and no `diff`
    # changed everything *except* its summary, which is worth saying — it's different from a doc
    # past DETAIL_LIMIT that nobody looked at.
    detailed: bool = False

    @property
    def name(self) -> str:
        """`specs/billing/refund-flow.md` → `billing/refund-flow.md`."""
        return _rel(self.path)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "purpose": self.purpose,
            "summary": self.summary,
            "diff": list(self.diff),
            "old_path": self.old_path,
            "detailed": self.detailed,
        }


@dataclass(frozen=True)
class Report:
    base: str
    added: tuple[DocChange, ...] = ()
    updated: tuple[DocChange, ...] = ()
    removed: tuple[DocChange, ...] = ()
    index_docs: tuple[str, ...] = ()
    history_docs: int = 0
    # Docs whose regenerated update the hook refused to write (generator.PENDING_DIR). Local state,
    # not part of the range — a draft is gitignored, so it exists on the machine that generated it
    # and nowhere else. Reported because a doc the hook wanted to change and couldn't is the one
    # thing a reviewer of this range cannot see anywhere else.
    pending_docs: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.added or self.updated or self.removed or self.index_docs)

    def as_dict(self) -> dict:
        return {
            "base": self.base,
            "added": [c.as_dict() for c in self.added],
            "updated": [c.as_dict() for c in self.updated],
            "removed": [c.as_dict() for c in self.removed],
            "index_docs": list(self.index_docs),
            "history_docs": self.history_docs,
            "pending_docs": list(self.pending_docs),
        }


def _diff_base(repo_root: Path, base: str) -> str:
    """The one revision both the file list and the old file contents are read from.

    `git diff base...HEAD` compares against the merge base, so a branch isn't credited with doc
    changes the target branch made after it forked. `git show` has no three-dot form, so resolve
    that merge base once here and use two dots everywhere else.
    """
    if base in (EMPTY_TREE, "HEAD"):
        return base
    try:
        return gitlog.run(repo_root, ["merge-base", base, "HEAD"]).strip() or base
    except subprocess.CalledProcessError:
        return base


def _changed_docs(repo_root: Path, rev: str) -> list[tuple[str, str, str]]:
    """`[(status letter, path, old path), ...]` for the doc paths this range changed."""
    out = gitlog.run(
        repo_root,
        ["diff", "--name-status", "-M", f"{rev}..HEAD", "--", paths.docs_prefix(repo_root)],
    )
    changed = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status, names = fields[0][:1], fields[1:]
        # A rename is `R100\told\tnew`; everything else is `<letter>\tpath`.
        path, old = (names[1], names[0]) if status == "R" and len(names) > 1 else (names[0], "")
        changed.append((status, path, old))
    return changed


def _show(repo_root: Path, rev: str, path: str) -> str:
    """A file's content at a revision, or "" when it didn't exist there."""
    if rev == EMPTY_TREE:
        return ""
    try:
        return gitlog.run(repo_root, ["show", f"{rev}:{path}"])
    except subprocess.CalledProcessError:
        return ""


def _summary_lines(content: str) -> list[str]:
    """The `## What It Does` section, blank lines collapsed away."""
    return [line.strip() for line in section(content, WHAT_IT_DOES) if line.strip()]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _summary(content: str) -> str:
    return _clip(" ".join(_summary_lines(content)), SUMMARY_MAX_CHARS)


def _section_diff(old: str, new: str) -> tuple[str, ...]:
    """The `## What It Does` change as `-`/`+` lines.

    Hunk headers are dropped: with `n=0` every line here is a change, and `@@ -3,2 +3,4 @@` tells a
    reader of prose nothing. An empty result means the section didn't change — which happens often,
    since a regeneration rewrites How It Works and the tables far more often than the summary.
    """
    lines = [
        line
        for line in difflib.unified_diff(_summary_lines(old), _summary_lines(new), n=0, lineterm="")
        if line[:1] in "-+" and not line.startswith(("---", "+++"))
    ]
    shown = [
        f"{line[0]}{_clip(line[1:].strip(), DIFF_MAX_COLS)}" for line in lines[:DIFF_MAX_LINES]
    ]
    if len(lines) > DIFF_MAX_LINES:
        shown.append(f"# … and {len(lines) - DIFF_MAX_LINES} more changed line(s)")
    return tuple(shown)


def run_pr_comment(repo_root: Path, base: str | None = None, since: str | None = None) -> Report:
    resolved = resolve_base(repo_root, base=base, since=since)
    rev = _diff_base(repo_root, resolved)
    purposes = modules_purposes(paths.modules_index(repo_root))
    history_prefix = paths.history_prefix(repo_root)

    added, updated, removed, index_docs, history = [], [], [], [], 0
    for status, path, old_path in sorted(_changed_docs(repo_root, rev), key=lambda c: c[1]):
        kind = _kind(path, history_prefix)
        if kind == "history":
            history += 1
            continue
        if kind == _INDEX_DOCS:
            index_docs.append(_rel(path))
            continue
        purpose = purposes.get(_rel(path), "")
        if status == "D":
            removed.append(DocChange(path, "removed", purpose))
        elif status == "A":
            added.append(DocChange(path, "added", purpose))
        else:
            updated.append(DocChange(path, "updated", purpose, old_path=old_path))

    # Only now, with the lists known, spend git processes — and only on the docs the comment will
    # actually detail. Both sides are read from git rather than the worktree: this describes a range
    # of commits, and it shouldn't change depending on what happens to be uncommitted locally.
    for i, change in enumerate(added[:DETAIL_LIMIT]):
        added[i] = DocChange(
            change.path,
            change.status,
            change.purpose,
            summary=_summary(_show(repo_root, "HEAD", change.path)),
            detailed=True,
        )
    for i, change in enumerate(updated[:DETAIL_LIMIT]):
        updated[i] = DocChange(
            change.path,
            change.status,
            change.purpose,
            diff=_section_diff(
                _show(repo_root, rev, change.old_path or change.path),
                _show(repo_root, "HEAD", change.path),
            ),
            old_path=change.old_path,
            detailed=True,
        )
    pending_dir = repo_root / PENDING_DIR
    return Report(
        base=resolved,
        added=tuple(added),
        updated=tuple(updated),
        removed=tuple(removed),
        index_docs=tuple(index_docs),
        history_docs=history,
        pending_docs=tuple(
            sorted(p.relative_to(pending_dir).as_posix() for p in pending_dir.rglob("*.md"))
        ),
    )


def _headline(report: Report) -> str:
    counts = [
        f"{len(report.added)} added",
        f"{len(report.updated)} updated",
        f"{len(report.removed)} removed",
    ]
    parts = [c for c in counts if not c.startswith("0 ")] or ["none"]
    return f"**Docs:** {', '.join(parts)} since `{report.base}`."


def _entry(change: DocChange) -> list[str]:
    """One bullet. The path is code-formatted rather than linked: a relative link in a PR *comment*
    isn't resolved against the repo the way one in a README is, so it would be a dead link."""
    purpose = f" — {change.purpose}" if change.purpose else ""
    renamed = f" (was `{_rel(change.old_path)}`)" if change.old_path else ""
    lines = [f"- **`{change.name}`**{purpose}{renamed}"]
    if change.summary:
        lines += ["", f"  > {change.summary}"]
    if change.diff:
        lines += ["", "  ```diff", *[f"  {line}" for line in change.diff], "  ```"]
    elif change.detailed and change.status == "updated":
        lines += ["", "  _Summary unchanged; the rest of the doc was edited._"]
    return lines


def _doc_section(title: str, changes: tuple[DocChange, ...]) -> list[str]:
    if not changes:
        return []
    lines = ["", f"### {title}", ""]
    for change in changes[:DOC_LIST_LIMIT]:
        lines += _entry(change)
    if len(changes) > DOC_LIST_LIMIT:
        lines += ["", f"…and {len(changes) - DOC_LIST_LIMIT} more."]
    return lines


def _pending_note(report: Report) -> list[str]:
    """The refused-draft warning, as its own block rather than an italic footnote.

    Louder than the other notes on purpose: every other line in this comment describes a doc change
    that happened, and this one describes one that was *stopped* — which means a doc in this range
    is knowingly behind the code, and the draft explaining how is on one machine only.
    """
    if not report.pending_docs:
        return []
    listed = ", ".join(f"`{doc}`" for doc in report.pending_docs[:DOC_LIST_LIMIT])
    more = (
        f" …and {len(report.pending_docs) - DOC_LIST_LIMIT} more"
        if len(report.pending_docs) > DOC_LIST_LIMIT
        else ""
    )
    return [
        "",
        f"> ⚠️ **{len(report.pending_docs)} doc update(s) were generated and refused** — {listed}"
        f"{more}. Writing them would have dropped hand-written content, or they named a flag this "
        f"CLI doesn't accept. The drafts are in `{PENDING_DIR}/` on the machine that ran the hook "
        "(gitignored, so they aren't in this range); `specky doctor` reports them until they're "
        "resolved.",
    ]


def comment_markdown(report: Report) -> str:
    """The whole comment body. Bounded — see this module's docstring."""
    if report.empty:
        head = f"**Docs:** no changes under `specs/` since `{report.base}`."
        return "\n".join([head, *_pending_note(report)]) + "\n"

    lines = [_headline(report)]
    lines += _doc_section("Added", report.added)
    lines += _doc_section("Updated", report.updated)
    lines += _doc_section("Removed", report.removed)

    notes = []
    if report.index_docs:
        notes.append(f"Index docs updated: {', '.join(f'`{d}`' for d in report.index_docs)}.")
    if report.history_docs:
        notes.append(
            f"{report.history_docs} per-commit doc(s) under `specs/history/` not listed."
        )
    detailed = min(DETAIL_LIMIT, len(report.added) + len(report.updated))
    if len(report.added) + len(report.updated) > detailed:
        notes.append(f"Summaries shown for the first {detailed} docs only.")
    if notes:
        lines += ["", *[f"_{note}_" for note in notes]]
    lines += _pending_note(report)
    lines += ["", "<sub>Generated by <code>specky pr-comment</code>.</sub>"]

    body = "\n".join(lines) + "\n"
    if len(body) <= BODY_MAX_CHARS:
        return body
    # Cut at a line boundary, and close a fence the cut landed inside — an unterminated ``` would
    # swallow the note below it into a code block.
    kept = body[:BODY_MAX_CHARS].rsplit("\n", 1)[0]
    if kept.count("```") % 2:
        kept += "\n  ```"
    return (
        f"{kept}\n\n_Trimmed: this range changed more docs than fit in one comment. "
        "Narrow the range, or run `specky pr-comment` locally to see all of them._\n"
    )
