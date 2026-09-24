"""`specky lint` — the doc-set problems no single doc shows: vocabulary, tags, and numbers that disagree.

Every check here is about the docs *together*, which is why none of them fits the write-time guards
in `generator.doc_problem` (one doc at a time) and why an agent following the `document-domain`
skill can't see them either — it writes one doc, carefully, and the drift is between that doc and
the thirty it didn't open. A real migration by agents shipped all three:

- **Undefined terms.** Docs using *Tolerance*, *Price variance* and *Matched peer* in five or nine
  places each while the glossary no longer defined any of them.
- **Tag sprawl.** Fifteen of twenty-five tags carried by one doc each — `manual-linking`,
  `document-matching`, `corrections` — because the agents couldn't run `specky tags` to see what
  existed, and a tag nobody else carries groups nothing.
- **Conflicting numbers.** The same threshold stated differently in two docs about the same thing.
  This one only catches numbers; the three conflicts that migration actually shipped were worded
  rules (a tie-break, a re-run's scope), which no offline check reads reliably. The skills' "one
  owner per rule" instruction is the defence for those, and this is the backstop for the kind a
  machine can compare.

Offline, reads the worktree (so it sees a doc written a minute ago, uncommitted), needs no index.
Everything it reports is advice; `--strict` turns any finding into exit 1 for a repo that wants it.
`specky check` runs the same functions over the docs a pull request is about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from specky import catalog, facts, paths
from specky.testgen import split_sections, tables

# Headings whose tables name states — statuses, outcomes, verdicts. The first column of such a table
# is vocabulary ("Price variance", "To control"), which is where undefined terms hide in plain
# sight: never bolded, so nothing else here would notice them.
_STATE_HEADING = re.compile(r"outcome|status|result|classification|diagnostic|verdict|state", re.I)

# A term the glossary lacks is only reported once this many docs use it. The glossary is shared
# vocabulary by definition (`document-domain` step 8 says a term local to one doc stays in that doc),
# and one doc's emphasis isn't drift.
MIN_DOCS_FOR_TERM = 2

# A backticked name that can hold a number: a snake_case or dotted field, or an ALL_CAPS constant
# (`price_gap`, `config.max_retries`, `VARIANCE_TOLERANCE_PCT`). A flag (`--yes`), a command or a
# plain word in backticks names an option or an action, and two docs giving `--yes` different
# numbers were describing two different commands' thresholds.
_QUANTITY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+|[A-Z][A-Z0-9_]{2,}")

# How far (in characters) a number may sit from the name it's taken to be about. Past this, the
# number belongs to some other clause of the sentence.
CLAIM_DISTANCE = 40

# How many findings of each kind the text report lists; `--json` carries all of them.
LIST_LIMIT = 15


@dataclass(frozen=True)
class UndefinedTerm:
    term: str
    docs: tuple[str, ...]  # every doc that uses it

    def as_dict(self) -> dict:
        return {"term": self.term, "docs": list(self.docs)}


@dataclass(frozen=True)
class TagProblem:
    tag: str
    docs: tuple[str, ...]
    problem: str  # "unregistered" (a registry exists and lacks it) or "single" (no registry)

    def as_dict(self) -> dict:
        return {"tag": self.tag, "docs": list(self.docs), "problem": self.problem}


@dataclass(frozen=True)
class Claim:
    doc: str
    value: str
    line: str


@dataclass(frozen=True)
class Conflict:
    key: str  # the name or glossary term the numbers are about
    claims: tuple[Claim, ...]  # one per (doc, value), across the disagreeing docs

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "claims": [{"doc": c.doc, "value": c.value, "line": c.line} for c in self.claims],
        }


@dataclass
class LintReport:
    docs: int = 0
    registry: bool = False
    undefined_terms: list[UndefinedTerm] = field(default_factory=list)
    tag_problems: list[TagProblem] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def findings(self) -> int:
        return len(self.undefined_terms) + len(self.tag_problems) + len(self.conflicts)

    def as_dict(self) -> dict:
        return {
            "docs": self.docs,
            "registry": self.registry,
            "undefined_terms": [t.as_dict() for t in self.undefined_terms],
            "tag_problems": [t.as_dict() for t in self.tag_problems],
            "conflicts": [c.as_dict() for c in self.conflicts],
        }


@dataclass(frozen=True)
class Doc:
    path: str
    tags: tuple[str, ...]
    body: str


def _plain(cell: str) -> str:
    """A table cell's text: links unwrapped, bold and backticks dropped. Underscores stay — they're
    part of `AUTO_RESOLVED_FEE`, and emphasis by `_x_` is rare enough in a status column to ignore."""
    return re.sub(r"[*`]", "", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cell)).strip()


def term_candidates(body: str) -> tuple[set[str], set[str]]:
    """What a doc uses as a name, as `(bold terms, status labels)`.

    Bold terms are `facts.extract`'s `term`s — an author's own signal that a phrase names something.
    Status labels are the first column of a table under an Outcomes/Status/Verdict-style heading,
    which is where statuses ("Price variance", "To control") live, never bolded. A label written as
    code (`` `specky tags` ``) is a command or a value, not vocabulary, and is left out.
    """
    bold = {f.key for f in facts.extract(body) if f.kind == "term"}
    labels: set[str] = set()
    for heading, lines in split_sections(body):
        if not heading or not _STATE_HEADING.search(heading) or facts.is_example_section(heading):
            continue
        for _, rows in tables(lines):
            for row in rows:
                if not row or row[0].lstrip("*").startswith("`"):
                    continue
                label = _plain(row[0])
                if label and facts.is_term_candidate(label):
                    labels.add(label)
    return bold, labels


def _headings(docs: list[Doc]) -> set[str]:
    return {
        facts.term_key(heading)
        for doc in docs
        for heading, _ in split_sections(doc.body)
        if heading
    }


def undefined_terms(
    docs: list[Doc], glossary: dict[str, str], scope: set[str], glossary_text: str = ""
) -> list[UndefinedTerm]:
    """Names docs in `scope` use that `MIN_DOCS_FOR_TERM`+ docs share and GLOSSARY.md doesn't define.

    Compared by `facts.term_key`, so "Regular entries" is covered by a **Regular entry** row, and a
    term counts as defined by any row it starts ("Tolerance" by "Tolerance (price)").

    "Share" depends on how the doc marked the name, measured against two real doc sets:

    - A **bold phrase** is the author saying "this is a term", so any doc mentioning it counts —
      the doc that says "the matched peer" in passing relies on the reader knowing it.
    - A **status label**, or any **single word**, counts only in docs that also use it *as a name*.
      Outcomes tables are full of labels that are descriptions ("Left alone", "Nothing found"), and
      counting mentions of those — or of "Source", "Other" — buried the real findings.

    A section heading ("Acceptance tests", "Edge cases") is template furniture, never a term. A
    code-shaped status (`COMPLIANT`) is covered when the glossary mentions it anywhere: those are
    values of a defined term ("Gap classification: COMPLIANT, …"), not terms of their own.
    """
    defined = {facts.term_key(term) for term in glossary}
    defined |= {facts.term_key(term.split("(")[0]) for term in glossary}
    # "Entry / line item" defines both spellings.
    defined |= {facts.term_key(part) for term in glossary if " / " in term for part in term.split(" / ")}
    defined |= _headings(docs)
    indexes = {doc.path: facts.Index(doc.body) for doc in docs}
    marked = {doc.path: term_candidates(doc.body) for doc in docs}
    named_in = {
        path: {facts.term_key(t) for t in bold | labels} for path, (bold, labels) in marked.items()
    }
    candidates: dict[str, tuple[str, bool]] = {}  # term_key -> (spelling, bold anywhere)
    for doc in docs:
        if doc.path not in scope:
            continue
        bold, labels = marked[doc.path]
        for term in bold | labels:
            key = facts.term_key(term)
            if not key or key in defined:
                continue
            if _code_shaped(term) and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", glossary_text):
                continue
            spelling, was_bold = candidates.get(key, (term, False))
            candidates[key] = (spelling, was_bold or term in bold)
    out = []
    for key, (term, bold) in candidates.items():
        if bold and len(term.split()) > 1:
            users = tuple(path for path, index in indexes.items() if index.mentions(term))
        else:
            users = tuple(path for path, named in named_in.items() if key in named)
        if len(users) >= MIN_DOCS_FOR_TERM:
            out.append(UndefinedTerm(term, users))
    return sorted(out, key=lambda t: (-len(t.docs), t.term.lower()))


def _code_shaped(term: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]+", term))


def tag_problems(docs: list[Doc], registry: dict[str, str], scope: set[str]) -> list[TagProblem]:
    """Tags on docs in `scope` that the vocabulary doesn't back.

    With a `TAGS.md`, that's a tag it doesn't list — whatever its count, since the registry is the
    decision about what a tag may be. Without one, it's a tag only one doc carries: the sprawl
    signal, because a tag that groups one doc groups nothing.
    """
    by_tag: dict[str, list[str]] = {}
    for doc in docs:
        for tag in doc.tags:
            by_tag.setdefault(tag, []).append(doc.path)
    registered = {tag.lower() for tag in registry}
    out = []
    for tag, carriers in sorted(by_tag.items()):
        if not scope & set(carriers):
            continue
        if registry:
            if tag.lower() not in registered:
                out.append(TagProblem(tag, tuple(carriers), "unregistered"))
        elif len(carriers) == 1:
            out.append(TagProblem(tag, tuple(carriers), "single"))
    return out


def _claims(
    doc: Doc, glossary_terms: list[str]
) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """`({key: {value: line}}, {key: every value on a line naming key})` for `doc`'s prose.

    The first is what the doc *claims*: each significant number paired with the nearest name on its
    line (a quantity-shaped backticked identifier, or a glossary term), within `CLAIM_DISTANCE`. The
    second is looser — every number on any line the name appears on — and is only ever used to find
    *agreement*: "DPGF over 30 PDF pages, or more than 50 pages in total" agrees with another doc's
    "50 (DPGF)" even though its 50 sits too far from the word to be claimed for it.

    Prose and table rows only, never code (a formula block states its own numbers precisely, and
    pseudo-code's numbers are steps) and never example sections (their numbers are illustrations).
    """
    out: dict[str, dict[str, str]] = {}
    mentioned: dict[str, set[str]] = {}
    fence = False
    pattern = (
        re.compile(r"(?<!\w)(" + "|".join(re.escape(t) for t in glossary_terms) + r")(?!\w)", re.I)
        if glossary_terms
        else None
    )
    for heading, lines in split_sections(doc.body):
        if facts.is_example_section(heading):
            continue
        for line in lines:
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
                continue
            if fence or line.lstrip().startswith("#"):
                continue
            # (offset, name, is code) — a backticked name compares as written, a glossary term by
            # `facts.term_key`, so "Tolerance" and "tolerances" are one key.
            names = [
                (m.start(), m.group(1), True)
                for m in re.finditer(r"`([^`\n]+)`", line)
                if _QUANTITY_NAME.fullmatch(m.group(1))
            ]
            if pattern:
                names += [(m.start(), m.group(1), False) for m in pattern.finditer(line)]
            if not names:
                continue
            numbers = facts.significant_numbers(line)
            for _, name, code in names:
                key = name if code else facts.term_key(name)
                mentioned.setdefault(key, set()).update(facts.number_value(raw) for _, raw in numbers)
            for at, raw in numbers:
                if any(start <= at < start + len(name) + 2 for start, name, _ in names):
                    continue  # the number is inside the name (`net_30`, "Rule 2025")
                # The nearest name, before or after, and only a close one: "over 20 (standard) or
                # 50 (DPGF)" is about DPGF for the 50 and about nothing named for the 20.
                distance, name, code = min(
                    (min(abs(at - start), abs(at - (start + len(name)))), name, code)
                    for start, name, code in names
                )
                if distance > CLAIM_DISTANCE:
                    continue
                key = name if code else facts.term_key(name)
                out.setdefault(key, {}).setdefault(facts.number_value(raw), line.strip())
    return out, mentioned


def conflicting_constants(
    docs: list[Doc], glossary: dict[str, str], scope: set[str]
) -> list[Conflict]:
    """Names two docs sharing a tag attach *disjoint* numbers to.

    Disjoint, not merely different: a doc that says "tolerance is €0.01 or 0.5%" and another that
    only mentions the €0.01 agree, and so do two docs where either one's claimed value appears
    anywhere on the other's lines about that name. Two docs about the same concept (a shared tag)
    with no value in common for the same named thing are the ones worth a human's minute.
    """
    terms = sorted((t for t in glossary if len(t) > 3), key=len, reverse=True)
    parsed = {doc.path: _claims(doc, terms) for doc in docs}
    claims = {path: pair[0] for path, pair in parsed.items()}
    mentioned = {path: pair[1] for path, pair in parsed.items()}
    tags = {doc.path: set(doc.tags) for doc in docs}
    found: dict[str, dict[tuple[str, str], Claim]] = {}
    for i, a in enumerate(docs):
        for b in docs[i + 1 :]:
            if not (tags[a.path] & tags[b.path]) or not ({a.path, b.path} & scope):
                continue
            for key in claims[a.path].keys() & claims[b.path].keys():
                va, vb = claims[a.path][key], claims[b.path][key]
                if va.keys() & mentioned[b.path][key] or vb.keys() & mentioned[a.path][key]:
                    continue
                bucket = found.setdefault(key, {})
                for doc, values in ((a.path, va), (b.path, vb)):
                    for value, line in values.items():
                        bucket.setdefault((doc, value), Claim(doc, value, _clip(line)))
    return [Conflict(key, tuple(bucket.values())) for key, bucket in sorted(found.items())]


def _clip(line: str, limit: int = 140) -> str:
    line = " ".join(line.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def load_docs(repo_root: Path) -> list[Doc]:
    return [
        Doc(path, tuple(catalog.doc_tags(meta)), body)
        for path, meta, body in catalog.docs_on_disk(repo_root)
    ]


def run_lint(repo_root: Path, only: set[str] | None = None) -> LintReport:
    """Every finding that involves a doc in `only` (repo-relative paths), or any doc when None."""
    docs = load_docs(repo_root)
    scope = {d.path for d in docs} if only is None else set(only)
    # The raw table rather than `html_render.load_glossary`: only the terms matter here, and that
    # import (markdown, jinja2, the whole renderer) would land on every `specky check`.
    glossary_path = paths.glossary(repo_root)
    glossary = paths.read_term_table(glossary_path)
    glossary_text = glossary_path.read_text(errors="replace") if glossary_path.exists() else ""
    registry = catalog.load_tag_registry(repo_root)
    return LintReport(
        docs=len(scope & {d.path for d in docs}),
        registry=bool(registry),
        undefined_terms=undefined_terms(docs, glossary, scope, glossary_text),
        tag_problems=tag_problems(docs, registry, scope),
        conflicts=conflicting_constants(docs, glossary, scope),
    )


def resolve_paths(repo_root: Path, cwd: Path, given: list[str]) -> set[str]:
    """CLI paths → repo-relative doc paths. A directory means every doc under it."""
    out: set[str] = set()
    for raw in given:
        path = (cwd / raw).resolve()
        if not path.is_relative_to(repo_root.resolve()):
            raise ValueError(f"{raw} is outside this repo")
        files = sorted(path.rglob("*.md")) if path.is_dir() else [path]
        for file in files:
            if not file.exists():
                raise ValueError(f"{raw} doesn't exist")
            out.add(file.relative_to(repo_root.resolve()).as_posix())
    return out


def _docs_list(docs: tuple[str, ...], limit: int = 3) -> str:
    shown = ", ".join(docs[:limit])
    return shown + (f" and {len(docs) - limit} more" if len(docs) > limit else "")


def section_lines(report: LintReport, *, prefix: str = "") -> list[str]:
    """The findings as text blocks, each opening with `prefix` — shared with `check`'s report."""
    lines: list[str] = []
    if report.undefined_terms:
        lines += [
            "",
            f"{prefix}{len(report.undefined_terms)} term(s) used across docs that GLOSSARY.md "
            "doesn't define — add a row for each (or reword to a term it does define):",
        ]
        lines += [
            f"  {t.term} — {len(t.docs)} docs: {_docs_list(t.docs)}"
            for t in report.undefined_terms[:LIST_LIMIT]
        ]
        if len(report.undefined_terms) > LIST_LIMIT:
            lines.append(f"  … and {len(report.undefined_terms) - LIST_LIMIT} more")
    if report.tag_problems:
        unregistered = [t for t in report.tag_problems if t.problem == "unregistered"]
        single = [t for t in report.tag_problems if t.problem == "single"]
        if unregistered:
            lines += [
                "",
                f"{prefix}{len(unregistered)} tag(s) not in TAGS.md — reuse a registered tag, or add "
                "a row if this is a genuinely new concept:",
            ]
            lines += [f"  {t.tag} — {_docs_list(t.docs)}" for t in unregistered[:LIST_LIMIT]]
            if len(unregistered) > LIST_LIMIT:
                lines.append(f"  … and {len(unregistered) - LIST_LIMIT} more")
        if single:
            lines += [
                "",
                f"{prefix}{len(single)} tag(s) carried by one doc only, so they group nothing — merge "
                "each into a shared tag (`specky tags --write` starts a TAGS.md registry to curate):",
            ]
            lines += [f"  {t.tag} — {t.docs[0]}" for t in single[:LIST_LIMIT]]
            if len(single) > LIST_LIMIT:
                lines.append(f"  … and {len(single) - LIST_LIMIT} more")
    if report.conflicts:
        lines += [
            "",
            f"{prefix}{len(report.conflicts)} name(s) given different numbers by docs that share a "
            "tag — state each once, in the doc that owns it, and link to it from the others:",
        ]
        for conflict in report.conflicts[:LIST_LIMIT]:
            lines.append(f"  {conflict.key}")
            lines += [f"    {c.doc}: {c.value} — {c.line}" for c in conflict.claims]
        if len(report.conflicts) > LIST_LIMIT:
            lines.append(f"  … and {len(report.conflicts) - LIST_LIMIT} more")
    return lines


def report_lines(report: LintReport) -> list[str]:
    head = f"specky lint: {report.docs} doc(s) checked, {report.findings} finding(s)"
    body = section_lines(report)
    return [head, *body] if body else [head, "", "Nothing to report."]
