"""`specky adopt` — bring a repo's existing markdown into the docs tree, once.

The problem it solves is specific. `generator.ExistingDocs.load()` walks the docs root and nothing
else, so the classification prompt's "docs that already exist" list is blind to a `docs/` tree an
team has been writing for years. Run `specky sync` on such a repo and it dutifully invents
`specs/billing/refunds.md` next to the `docs/billing/refunds.md` that already says the same thing —
and the prompt itself calls a second doc on one subject a defect. Adoption *imports* those files
into the docs root rather than merely noting they exist, which is what makes them visible to every
part of specky at once: classification, the index, `specky check`, the viewer.

Three properties worth stating, because each is a decision:

- **No AI call, ever.** This is discovery, `git mv` and frontmatter. Classifying a doc (`type:`,
  `tags:`) is `specky tag`'s job and stays there, so adoption is free, instant and easy to trust.
- **Adopted docs are frozen.** Each one gets `authored: human`, the existing marker that
  `generator.sync_feature_doc` already honours: a doc somebody wrote by hand is not something the
  post-commit hook should rewrite the week after it's imported. Clearing the line opts back in.
- **Nothing is committed.** A one-time import that rearranges a repo's documentation is exactly
  the change a human should read in `git status` before it becomes history.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from specky import frontmatter, paths

# Directories whose markdown is documentation by convention. First path segment, except
# `decisions`/`adr`, which are usually nested (`docs/adr/`, `architecture/decisions/`).
DOC_DIRS = ("docs", "doc", "documentation", "adr", "rfc", "rfcs")
DOC_DIR_ANYWHERE = ("adr", "adrs", "decisions", "rfc", "rfcs")

# Root-level files that are documentation despite not being in a docs directory. Each becomes
# `<stem>/overview.md`, the same shape a folder-level README gets. An allowlist rather than a
# denylist of furniture, because the rest of the root is furniture: `README.md`, `CONTRIBUTING.md`,
# `CHANGELOG.md` and friends address someone arriving at the repository rather than someone reading
# about the system, and moving one breaks the rendering a forge does of it at a known path. Adoptable
# anyway with an explicit `--include README.md`.
ROOT_DOCS = ("ARCHITECTURE.md", "DESIGN.md", "RUNBOOK.md", "OPERATIONS.md")

# A folder's own index doc, which becomes `<domain>/overview.md` rather than `<domain>/readme.md`.
INDEX_STEMS = ("readme", "index", "overview")

# Above this many files, adoption stops to confirm. Not about money — nothing here is billable —
# but about scale: this rewrites where a repo's documentation lives, and someone who meant to
# adopt one directory should find out before 300 files move.
ADOPT_CONFIRM_THRESHOLD = 20


@dataclass(frozen=True)
class Adoption:
    source: str  # repo-relative, as git spells it
    dest: str  # repo-relative, under the docs root


@dataclass
class Report:
    adopted: list[Adoption] = field(default_factory=list)
    # (source, why) for a file that was found but deliberately not imported. Reported, never
    # resolved automatically — see `_plan`.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout


def kebab(text: str) -> str:
    """`Refund Flow (v2)` → `refund-flow-v2`; `RefundFlow` → `refund-flow`.

    The split before an interior capital is what keeps a `CamelCase.md` filename from adopting as
    one long unreadable word, which is most of what an older docs tree is named like.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text)
    return re.sub(r"[^a-z0-9]+", "-", spaced.lower()).strip("-")


def _tracked_markdown(repo_root: Path) -> list[str]:
    """Every tracked `.md` path, from git.

    `git ls-files` rather than an `rglob`, and this is the load-bearing choice in discovery: it
    can't see anything gitignored or untracked, so `node_modules/`, a `.venv/`, a vendored
    dependency's docs and specky's own `.specky/` are excluded by construction rather than by a
    denylist that would need a new entry per ecosystem.
    """
    return sorted(f for f in _git(repo_root, "ls-files", "--", "*.md", "*.markdown").splitlines() if f)


def discover(
    repo_root: Path,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> list[str]:
    """The repo's existing documentation, as repo-relative paths.

    `include` globs add files the conventions below wouldn't have found (and override the furniture
    exclusion); `exclude` globs win over everything, so a repo can adopt `docs/` while leaving
    `docs/vendor/` where it is.
    """
    docs_prefix = paths.docs_prefix(repo_root)
    found = []
    for path in _tracked_markdown(repo_root):
        if path.startswith(docs_prefix) or any(fnmatch(path, pat) for pat in exclude):
            continue
        if any(fnmatch(path, pat) for pat in include):
            found.append(path)
            continue
        parts = Path(path).parts
        if len(parts) == 1:
            if path in ROOT_DOCS:  # furniture and anything else at the root is left alone
                found.append(path)
            continue
        if parts[0].lower() in DOC_DIRS or any(p.lower() in DOC_DIR_ANYWHERE for p in parts[:-1]):
            found.append(path)
    return found


def destination(repo_root: Path, source: str, domain_override: str | None = None) -> str:
    """Where `source` lands: `<docs root>/<domain>/<topic>.md`.

    The mapping keeps whatever structure the old tree already had, because that structure is
    someone's considered filing decision and re-deriving it would need the AI call this command
    doesn't make:

    - `docs/billing/refunds.md` → `billing/refunds.md`. The segment below the docs directory is the
      domain, which is the same thing specky's own classification means by one.
    - `docs/billing/api/refunds.md` → `billing/api-refunds.md`. Deeper nesting folds into the topic
      rather than the domain: a domain is one level by definition (`indexer._domain_for`), and
      dropping the middle segments would collide two docs onto one name.
    - `docs/billing/README.md` → `billing/overview.md`. A folder's index doc is about the folder, so
      the folder's own name supplies the domain and the topic is just `overview`.
    - `ARCHITECTURE.md` → `architecture/overview.md`. Same idea one level up. A root-level *index*
      stem (only reachable via an explicit `--include README.md`) has no name of its own to use, so
      it takes the repo directory's — that file is about the repo.
    - `docs/refunds.md` → `docs/refunds.md`, i.e. domain `docs`: with nothing between the docs
      directory and the file there's no domain to read, so the directory's own name is it. A flat
      `docs/` tree is the case where `--domain` earns its keep.
    """
    root = paths.docs_root(repo_root).name
    parts = list(Path(source).parts)
    stem = Path(parts[-1]).stem
    middle = parts[1:-1]  # between the top-level directory and the file itself
    index_doc = stem.lower() in INDEX_STEMS
    domain_override = kebab(domain_override) if domain_override else None

    if len(parts) == 1:  # a root-level doc: ARCHITECTURE.md and friends
        domain = domain_override or kebab(repo_root.name if index_doc else stem)
        return f"{root}/{domain}/overview.md"

    if index_doc:
        return f"{root}/{domain_override or kebab(middle[-1] if middle else parts[0])}/overview.md"

    domain = domain_override or kebab(middle[0] if middle else parts[0])
    topic = "-".join(kebab(p) for p in [*middle[1:], stem])
    return f"{root}/{domain}/{topic}.md"


def _plan(
    repo_root: Path, sources: list[str], domain_override: str | None
) -> tuple[list[Adoption], list[tuple[str, str]]]:
    """Pair each source with its destination, setting aside the ones that can't be imported.

    A destination that's already taken is **skipped and reported**, never renamed to a free name.
    An automatic `-2` suffix here would produce exactly the duplicate this whole command exists to
    prevent, only harder to spot — two docs on one subject with near-identical names. The human
    picks: `--domain`, a rename, or a merge.
    """
    adopted: list[Adoption] = []
    skipped: list[tuple[str, str]] = []
    claimed: dict[str, str] = {}
    for source in sources:
        dest = destination(repo_root, source, domain_override)
        if (repo_root / dest).exists():
            skipped.append((source, f"{dest} already exists"))
        elif dest in claimed:
            skipped.append((source, f"{dest} is already taken by {claimed[dest]}"))
        else:
            claimed[dest] = source
            adopted.append(Adoption(source, dest))
    return adopted, skipped


def adopted_content(source_text: str, source_path: str) -> str:
    """The doc as it will be written: its own content, with two frontmatter keys added.

    Every existing key is preserved — an older tree may already carry an `owner:` or a `related:`,
    and those are the fields specky would otherwise ask a human to supply. `type`/`tags` are left
    unset so `specky tag` fills them in the next step.
    """
    meta, body = frontmatter.parse(source_text)
    meta = dict(meta)
    meta.setdefault("authored", "human")  # the freeze generator.sync_feature_doc honours
    meta.setdefault("origin", source_path)  # so a stale link to the old path can be resolved
    return frontmatter.render(meta, body)


def _stub(source: str, dest: str) -> str:
    """The one-line pointer left behind by `--stub`, with a link that works on disk and on a forge."""
    relative = os.path.relpath(dest, Path(source).parent)
    return f"# Moved\n\nThis document now lives at [{dest}]({relative}).\n"


def _confirm(count: int, assume_yes: bool) -> None:
    if assume_yes or count < ADOPT_CONFIRM_THRESHOLD:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{count} files is over the {ADOPT_CONFIRM_THRESHOLD}-file confirmation threshold and "
            "stdin isn't a terminal — re-run with --yes, or narrow it with --include/--exclude"
        )
    answer = input(f"specky adopt: import {count} files into the docs tree? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise RuntimeError("cancelled")


def run_adopt(
    repo_root: Path,
    mode: str = "move",
    domain: str | None = None,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    dry_run: bool = False,
    assume_yes: bool = False,
) -> Report:
    """Import the repo's existing markdown into the docs tree. Writes files, commits nothing.

    `mode` is `move` (the default — `git mv`, so the history follows the file and the whole import
    is one `git checkout` away from undone), `keep` (copy, leaving the original in place) or `stub`
    (move, leaving a one-line pointer behind for anything linking to the old path).
    """
    from specky.generator import _doc_title, update_modules_index

    if mode not in ("move", "keep", "stub"):
        raise ValueError(f"unknown mode {mode!r} — expected move, keep or stub")

    sources = discover(repo_root, include=include, exclude=exclude)
    adopted, skipped = _plan(repo_root, sources, domain)
    report = Report(adopted=adopted, skipped=skipped, dry_run=dry_run)
    if dry_run or not adopted:
        return report

    _confirm(len(adopted), assume_yes)

    for item in adopted:
        source_path = repo_root / item.source
        dest_path = repo_root / item.dest
        content = adopted_content(source_path.read_text(), item.source)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if mode == "keep":
            dest_path.write_text(content)
        else:
            # `git mv` before rewriting, so git records a rename rather than a delete plus an
            # unrelated add — which is what keeps `git log --follow` (and specky's own staleness
            # dates) working across the import.
            _git(repo_root, "mv", item.source, item.dest)
            dest_path.write_text(content)
            if mode == "stub":
                source_path.write_text(_stub(item.source, item.dest))

        meta, body = frontmatter.parse(content)
        rel = item.dest.split("/", 1)[-1]  # MODULES.md links are relative to the docs root
        update_modules_index(
            repo_root, rel.split("/")[0], rel, _doc_title(body, Path(item.dest).stem)
        )
    return report


def report_lines(report: Report) -> list[str]:
    verb = "would import" if report.dry_run else "imported"
    lines = [f"specky adopt: {verb} {len(report.adopted)} file(s)"]
    lines += [f"  {a.source} → {a.dest}" for a in report.adopted]
    if report.skipped:
        lines.append("")
        lines.append(f"Skipped {len(report.skipped)} file(s) — resolve by hand, nothing was moved:")
        lines += [f"  {source}: {reason}" for source, reason in report.skipped]
    if report.adopted and not report.dry_run:
        lines.append("")
        lines.append("Nothing was committed. Review `git status`, then:")
        lines.append("  specky tag       # fill in type:/tags: on the imported docs")
        lines.append("  specky index     # pick them up for search, check and the viewer")
        lines.append("  specky sync --since <a recent revision>")
        lines.append(
            "The last one narrowly on purpose: this repo is already documented, so a full-history "
            "sync would pay to describe commits these docs already cover."
        )
    return lines
