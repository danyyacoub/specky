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

`--verify` is the other half of a migration, for the repo that didn't import its old docs but had
them *rewritten* — an agent reading the old tree, checking it against the code and writing a new
doc per topic. Nothing in that loop notices a scoring weight or a formula that didn't make it
across, and a real migration lost dozens that way. `run_verify` pairs each old doc with the new one
that replaced it, the same way an import would have placed it, and reports every fact
(`facts.py`) the old one stated that no doc in the new tree states now. Same three properties:
no AI call, nothing written, and the report is for a human to act on.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from specky import facts, frontmatter, paths

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

# Root files of a docs tree that pair with their counterpart in the docs root rather than mapping
# like a topic doc would (`specs/GLOSSARY.md` → `<root>/specs/glossary.md` names nothing). Checked
# by exact name, since these are specky's own file names.
ROOT_COUNTERPARTS = {"GLOSSARY.md": paths.glossary, "PRODUCT.md": paths.product_doc, "MODULES.md": paths.modules_index}

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
    only: tuple[str, ...] = (),
) -> list[str]:
    """The repo's existing documentation, as repo-relative paths.

    `include` globs add files the conventions below wouldn't have found (and override the furniture
    exclusion); `exclude` globs win over everything, so a repo can adopt `docs/` while leaving
    `docs/vendor/` where it is. `only` replaces the conventions outright — the files matching it and
    nothing else — for the run that is about one tree: importing a single directory, or verifying
    that an old `specs/` made it across without `docs/` and `DESIGN.md` joining the report.
    """
    docs_prefix = paths.docs_prefix(repo_root)
    found = []
    for path in _tracked_markdown(repo_root):
        if path.startswith(docs_prefix) or any(fnmatch(path, pat) for pat in exclude):
            continue
        if only:
            if any(fnmatch(path, pat) for pat in only):
                found.append(path)
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
    only: tuple[str, ...] = (),
) -> Report:
    """Import the repo's existing markdown into the docs tree. Writes files, commits nothing.

    `mode` is `move` (the default — `git mv`, so the history follows the file and the whole import
    is one `git checkout` away from undone), `keep` (copy, leaving the original in place) or `stub`
    (move, leaving a one-line pointer behind for anything linking to the old path).
    """
    from specky.generator import _doc_title, update_modules_index

    if mode not in ("move", "keep", "stub"):
        raise ValueError(f"unknown mode {mode!r} — expected move, keep or stub")

    sources = discover(repo_root, include=include, exclude=exclude, only=only)
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


# --- verify ---------------------------------------------------------------------------------------


@dataclass
class VerifiedDoc:
    source: str
    # The doc in the docs tree that replaced `source`, or None when nothing did.
    counterpart: str | None
    stated: int  # how many facts `source` states
    missing: list[facts.Fact] = field(default_factory=list)  # stated nowhere in the docs tree now
    moved: list[tuple[facts.Fact, str]] = field(default_factory=list)  # (fact, doc that states it)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "counterpart": self.counterpart,
            "facts": self.stated,
            "missing": [f.as_dict() | {"constant": f.constant} for f in self.missing],
            "moved": [f.as_dict() | {"constant": f.constant, "to": to} for f, to in self.moved],
        }


@dataclass
class VerifyReport:
    docs: list[VerifiedDoc] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"docs": [d.as_dict() for d in self.docs]}


def _tree(repo_root: Path) -> dict[str, str]:
    """Every doc in the docs tree but `history/`, as `{repo-relative path: text}`."""
    root = paths.docs_root(repo_root)
    history = paths.history_dir(repo_root)
    if not root.exists():
        return {}
    return {
        p.relative_to(repo_root).as_posix(): p.read_text(errors="replace")
        for p in sorted(root.rglob("*.md"))
        if history not in p.parents
    }


def counterpart(repo_root: Path, source: str, tree: dict[str, str], domain: str | None) -> str | None:
    """The doc in the docs tree that replaced `source`, in the order an import would decide it.

    1. A doc whose `origin:` names `source` — `specky adopt` wrote that pointer, and a human who
       moved the doc since has kept it, so it outranks any mapping.
    2. A root `GLOSSARY.md` / `PRODUCT.md` / `MODULES.md` of the old tree — the new tree's own one.
    3. `destination()`, the path an import would have put it at.
    """
    for path, text in tree.items():
        meta, _ = frontmatter.parse(text)
        if str(meta.get("origin", "")).strip() == source:
            return path
    name = Path(source).name
    if name in ROOT_COUNTERPARTS and len(Path(source).parts) <= 2:
        return ROOT_COUNTERPARTS[name](repo_root).relative_to(repo_root).as_posix()
    dest = destination(repo_root, source, domain)
    return dest if dest in tree else None


def run_verify(
    repo_root: Path,
    domain: str | None = None,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    only: tuple[str, ...] = (),
) -> VerifyReport:
    """What each old doc said that the docs tree doesn't say anymore. Reads only; writes nothing.

    A fact the counterpart dropped is looked for across the whole tree before it's called missing:
    a rewrite that splits one old doc into two, or moves a formula to the doc that owns it, has
    lost nothing, and a report that said otherwise would be one nobody trusts twice. An old doc with
    no counterpart at all (a `FORMULAS.md` whose contents were spread across topic docs) is checked
    the same way, fact by fact.
    """
    tree = _tree(repo_root)
    indexes = {path: facts.Index(text) for path, text in tree.items()}
    report = VerifyReport()
    for source in discover(repo_root, include=include, exclude=exclude, only=only):
        text = (repo_root / source).read_text(errors="replace")
        stated = facts.extract(text)
        paired = counterpart(repo_root, source, tree, domain)
        dropped = facts.missing_from(stated, indexes[paired]) if paired else stated
        others = {path: index for path, index in indexes.items() if path != paired}
        doc = VerifiedDoc(source, paired, len(stated))
        for fact in dropped:
            found = facts.locate(fact, others)
            if found:
                doc.moved.append((fact, found))
            else:
                doc.missing.append(fact)
        report.docs.append(doc)
    return report


# How many of a doc's missing *names* the report lists; its missing constants are always all shown.
VERIFY_NAMES_SHOWN = 20


def verify_lines(report: VerifyReport) -> list[str]:
    """The report as markdown — the review artifact a migration hands a human.

    Missing constants are listed in full with the line they came from, because each one is a
    decision to make: restore it, or confirm the code dropped it. Missing names are listed
    compactly, and moved facts are counted per destination — both are context, not findings.
    """
    docs = report.docs
    missing_constants = sum(1 for d in docs for f in d.missing if f.constant)
    missing_names = sum(1 for d in docs for f in d.missing if not f.constant)
    unpaired = sum(1 for d in docs if d.counterpart is None)
    lines = [
        "# specky adopt --verify",
        "",
        f"{len(docs)} source doc(s) checked; {unpaired} with no counterpart in the docs tree. "
        f"{missing_constants} constant(s) and {missing_names} name(s) they state appear nowhere "
        "in the docs tree now.",
    ]
    clean = [d for d in docs if not d.missing]
    for doc in docs:
        if not doc.missing:
            continue
        target = doc.counterpart or "no counterpart"
        lines += ["", f"## {doc.source} → {target}", ""]
        constants = [f for f in doc.missing if f.constant]
        names = [f for f in doc.missing if not f.constant]
        if constants:
            lines.append(f"Missing constants ({len(constants)}):")
            for fact in constants:
                where = f" _({fact.section})_" if fact.section else ""
                context = "" if fact.kind == "formula" else f" — `{fact.line}`"
                lines.append(f"- {fact.kind} {fact.label()}{where}{context}")
        if names:
            if constants:
                lines.append("")
            shown = ", ".join(f.label() for f in names[:VERIFY_NAMES_SHOWN])
            more = len(names) - VERIFY_NAMES_SHOWN
            lines.append(
                f"Missing names ({len(names)}): {shown}" + (f" and {more} more" if more > 0 else "")
            )
        if doc.moved:
            by_target: dict[str, int] = {}
            for _, to in doc.moved:
                by_target[to] = by_target.get(to, 0) + 1
            spread = ", ".join(f"{to} ({n})" for to, n in sorted(by_target.items(), key=lambda x: -x[1]))
            lines += ["", f"Found elsewhere: {spread}"]
    if clean:
        lines += ["", f"Nothing missing from {len(clean)} doc(s): " + ", ".join(d.source for d in clean)]
    return lines
