"""Keeps specs/<domain>/<topic>.md in sync with what a feature/workflow currently does.

This is the *maintenance* half of specky's doc writing, and `document.py` is the authoring half:
there, a model searches the repo and writes a feature's doc from the code; here, the same doc is
kept current from the commits that touch it afterwards. The split is why this module never reads
source — by the time it runs, the doc exists and a diff is a precise statement of what changed
about it. It runs unattended off the configured AI provider, triggered per commit by commit_doc.py. It classifies whether a commit's diff affects a documented
feature/workflow, and if so, generates or updates that feature's reference doc in
place — these specs/<domain>/*.md files are the reference, not the per-commit log in
specs/history/ (which stays as a supplementary changelog trail).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from specky import frontmatter, paths
from specky.ai_provider import Provider
from specky.commit_doc import DIFF_TRUNCATE_CHARS, Commit
from specky.testgen import split_sections

# Docs excluded from type/tags classification — root-level meta docs and the per-commit
# changelog trail aren't feature/workflow reference docs.
_UNTAGGED_DOMAINS = {"root", "history"}

# Where a regeneration this module refused to write is parked instead. Under `.specky/`, which is
# gitignored, so a rejected rewrite can never reach a commit — and `specky doctor` warns for as
# long as one is sitting there.
PENDING_DIR = ".specky/pending"

# How much of a section (or of the whole doc) a regeneration may drop before it counts as a rewrite
# rather than an update. Measured against this repo's own history rather than guessed: across 33
# honest doc updates, no section ever fell below 98% of its previous size, while the two
# destructive rewrites ran 46–71% with sections missing outright. 0.8 sits in that gap with room on
# both sides. A false positive costs a refused write and a warning; a false negative costs prose
# nobody notices is gone, which is what happened twice.
MIN_KEPT_RATIO = 0.8
# Sections smaller than this are exempt from the ratio. A three-line section losing half its
# characters is noise; the losses worth stopping were thousands of characters each.
MIN_SECTION_CHARS = 400

# A long-form CLI flag as it appears in prose. The lookbehind keeps `--foo` out of `x---foo` and
# stops mid-word matches inside an already-matched flag.
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")

# A MODULES.md table row: `| [billing/refund-flow.md](billing/refund-flow.md) | Issue refunds |`.
_MODULES_ROW = re.compile(r"^\|\s*\[[^\]]*\]\(([^)]+)\)\s*\|([^|]*)\|")

TAG_GUIDANCE = """- "type": "feature" (a bounded capability) or "workflow" (a multi-step process).
- "tags": 1-3 kebab-case tags describing the business/domain concept (e.g. "billing", "refunds",
  "onboarding") — not implementation details (not "sqlite", "regex", "fts5"). Prefer reusing one of
  the existing tags below over inventing a new one; only add a new tag if none of these fit.
Existing tags in use: {existing_tags}"""

CLASSIFY_PROMPT = """You maintain a set of feature/workflow reference docs under specs/<domain>/<topic>.md \
for this codebase. Given a commit's message and diff, decide whether it changes user-facing feature or \
workflow behavior worth reflecting in that reference documentation. Skip pure refactors, formatting, \
dependency bumps, CI/config-only changes, and typo fixes with no behavior change.

Respond with ONLY a JSON object, no other text, matching exactly one of these shapes:
{{"skip": true}}
{{"skip": false, "domain": "kebab-case-domain", "topic": "kebab-case-topic", "purpose": "one-line description", \
"type": "feature|workflow", "tags": ["tag1", "tag2"]}}

- "domain": the module/area this belongs to (e.g. "billing", "auth", "search").
- "topic": a short kebab-case slug for the specific feature/workflow (e.g. "refund-flow", "rate-limits").
- "purpose": one short sentence describing what that feature/workflow does, for an index table.
- If one of the docs listed below already covers what this commit changed, answer with that doc's
  exact "domain" and "topic" so it gets updated in place. Only invent a new domain/topic pair when
  none of them is about this feature/workflow — a second doc on the same subject is a defect.

Docs that already exist (domain/topic — what it covers):
{existing_docs}
""" + TAG_GUIDANCE

# Split in two on purpose, and this is the boundary that matters most in specky. Everything above
# is identical for every commit in a run — the instructions, and the list of every doc that already
# exists. Everything below changes per commit. A run over 400 commits therefore resends the same
# several-kilobyte preamble 400 times, and on a repo with 300 docs that preamble is the largest
# single thing specky pays for: the classification bill is O(commits x docs).
#
# So the top half is handed to `Provider.generate` as `prefix`, which providers that support prompt
# caching bill at a tenth of the input rate after the first call. Caching is a literal prefix match,
# so the commit-specific half has to come strictly after — which is how this prompt was already
# laid out, because it also reads better that way.
CLASSIFY_COMMIT = """Commit message:
{message}

Diff (may be truncated):
{diff}
"""

DOC_STYLE_INSTRUCTIONS = """Write the doc in this style:

# {domain_title} — {topic_title}

## What It Does
2-3 sentences, plain language. Non-technical readers should understand this.

## How It Works
Numbered steps explaining the process. Each step is one sentence with a bold label.

## Outcomes
Table of possible outcomes/results, if the feature has distinct outcomes/statuses.

## Acceptance Tests
Given/When/Then table pinning down expected behaviour. If nothing testable, say so explicitly rather than
omitting the section.

Style: plain language, compact, focus on WHAT and WHY not implementation details, tables for structured
information, no code blocks except formulas/thresholds, understandable by non-technical stakeholders.
"""

# A workflow is a sequence, and the thing a reader needs from it is the order — so its doc is shaped
# around one: the happy path as numbered steps, the same path drawn once underneath them, and every
# way it can go otherwise gathered in one place instead of scattered through the steps as asides.
#
# Deliberately still `## How It Works` rather than `## Happy Path`. `lost_content` refuses a
# regeneration that drops a section, so a rename would leave every workflow doc already on disk
# carrying both headings — the old one frozen, because nothing would ever be asked to update it.
# What changes is the heading's scope, not its name.
WORKFLOW_STYLE_INSTRUCTIONS = """Write the doc in this style:

# {domain_title} — {topic_title}

## What It Does
2-3 sentences, plain language: what starts this flow, what it produces, and who or what it is for.
Non-technical readers should understand this.

## How It Works
The happy path and nothing else — the run where everything goes right. Numbered steps in the order
they happen, each one sentence with a bold label. Anything conditional, any failure and any refusal
belongs in Edge Cases below, not here.

Immediately after the numbered steps, a fenced ```mermaid``` block drawing that same path: a
`flowchart` where the shape is a sequence of moves, a `sequenceDiagram` where distinct actors hand
off to each other. Never both in one doc. Keep labels short, reuse the numbered steps' own wording,
and if the diagram and the steps ever disagree, the steps win.

## Outcomes
Table of the distinct end states this flow can reach, and what each one means.

## Edge Cases
Table: Situation | What happens | Why. Every branch off the happy path — what is refused, what is
skipped silently, what is retried, what a partial run leaves behind. If there genuinely are none,
say so in one line rather than omitting the section.

## Acceptance Tests
Given/When/Then table pinning down expected behaviour: the happy path, plus a row for every Edge
Cases row above. If nothing testable, say so explicitly rather than omitting the section.

Style: plain language, compact, focus on WHAT and WHY not implementation details, tables for structured
information, no code blocks except formulas/thresholds, understandable by non-technical stakeholders.
"""


def doc_style(doc_type: str, *, domain_title: str, topic_title: str) -> str:
    """The doc template for this classification, filled in.

    The one place the fallback is decided, and the fallback is the point: `doc_type` is `""` on any
    path that never classified, and the answer there has to be the shape that was there before
    workflows had their own — not a workflow doc's stricter one, which would ask an unclassified
    doc for a diagram and an Edge Cases table nobody decided it owed.

    `document.py` deliberately does *not* come through here. It has to show the model both templates
    up front, because the type is chosen mid-run and its prompt prefix is cached.
    """
    template = (
        WORKFLOW_STYLE_INSTRUCTIONS if doc_type == "workflow" else DOC_STYLE_INSTRUCTIONS
    )
    return template.format(domain_title=domain_title, topic_title=topic_title)


# The update path asks for sections rather than a whole file. A whole-file answer means every
# untouched paragraph is re-emitted from the model's reading of it, which is how hand-written
# detail gets quietly dropped and how prose gets reflowed into one line per paragraph (making
# `git diff specs/` useless for review). A section the model doesn't mention is copied through
# byte-for-byte instead.
SECTION_UPDATE_INSTRUCTIONS = """Respond with ONLY a JSON object, no other text:
{{"sections": {{"<heading>": "<that section's new markdown, without its `##` heading line>"}}}}

- Include ONLY the sections this commit actually changes. A section you leave out is kept exactly
  as it already is, which is the right outcome for anything this change didn't touch.
- To replace a section, use its heading exactly as listed below. To add one, use a new heading —
  it's appended at the end.
- A section you do rewrite must carry its existing content through. It may hold hand-written
  detail, tables or diagrams that are still correct; edit around them rather than summarising them
  away. A rewrite that shortens a section is rejected outright and the doc is left alone.
- Respond with {{"sections": {{}}}} if the doc is already accurate for this change.

Sections in the doc right now:
{section_list}
"""


@dataclass
class Classification:
    skip: bool
    domain: str = ""
    topic: str = ""
    purpose: str = ""
    reason: str = ""
    doc_type: str = ""
    tags: list[str] = field(default_factory=list)


def strip_code_fence(text: str) -> str:
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text.strip(), re.DOTALL)
    return match.group(1) if match else text.strip()


def leading_json_object(text: str) -> dict | None:
    """The JSON object `text` starts with, ignoring anything after it — or None if it starts with
    something else.

    Every JSON answer specky asks a model for goes through here rather than `json.loads`, which
    refuses trailing data. A model that gets the object right and then adds to it is the common
    failure: one closing brace too many on a 4kB `{"sections": ...}` envelope was enough to lose a
    whole section splice against a real repo, and the fallbacks are quiet — a dropped splice becomes
    a whole-doc rewrite that `lost_content` refuses, a dropped classification becomes a skipped
    commit, a dropped tag response leaves the doc untagged. None of those look like a parse error.
    """
    try:
        value, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _doc_title(body: str, fallback: str) -> str:
    return next((line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")), fallback)


def modules_purposes(modules_path: Path) -> dict[str, str]:
    """`{"billing/refund-flow.md": "Issue refunds"}` from MODULES.md's tables — the one place a
    doc's one-line purpose is already written down."""
    if not modules_path.exists():
        return {}
    rows = (_MODULES_ROW.match(line) for line in modules_path.read_text().splitlines())
    return {m.group(1).strip(): m.group(2).strip() for m in rows if m}


@dataclass
class ExistingDocs:
    """What's already documented, read off disk once per run.

    Both prompts steer the model toward reusing what exists — an existing tag over a
    near-duplicate, an existing domain/topic over a sibling doc on the same subject — so both
    need this. It's a snapshot passed in rather than a lookup per prompt because the walk it
    replaces ran once per doc inside backfill_tags' loop, re-reading every doc O(n^2) times.

    Read from `specs/` rather than the `documents` table on purpose: the post-commit hook runs
    before any re-index, so the index is routinely a commit behind what's on disk.
    """

    tags: set[str] = field(default_factory=set)
    purposes: dict[str, str] = field(default_factory=dict)  # "domain/topic" -> one-liner

    @classmethod
    def load(cls, repo_root: Path) -> ExistingDocs:
        snapshot = cls()
        specs_root = paths.docs_root(repo_root)
        if not specs_root.exists():
            return snapshot

        purposes = modules_purposes(paths.modules_index(repo_root))
        for md_path in sorted(specs_root.rglob("*.md")):
            rel = md_path.relative_to(specs_root)
            domain = rel.parts[0] if len(rel.parts) > 1 else "root"
            if domain in _UNTAGGED_DOMAINS:
                continue
            meta, body = frontmatter.parse(md_path.read_text())
            name = rel.with_suffix("").as_posix()
            tags = meta.get("tags")
            snapshot.tags.update(tags if isinstance(tags, list) else [])
            # MODULES.md is the source of the purpose when it has a row; otherwise the doc's own
            # H1 is the best one-liner available without asking the model about it.
            snapshot.purposes[name] = purposes.get(f"{name}.md") or _doc_title(body, md_path.stem)
        return snapshot

    def record(self, name: str, purpose: str, tags: list[str]) -> None:
        """Fold in a doc written during this run, so a later commit in the same `specky sync`
        sees it as existing rather than inventing a second doc for the same feature."""
        self.tags.update(tags)
        self.purposes[name] = purpose or self.purposes.get(name, "")

    def tags_line(self) -> str:
        return ", ".join(sorted(self.tags)) or "(none yet)"

    def docs_block(self) -> str:
        return "\n".join(f"- {name} — {purpose}" for name, purpose in sorted(self.purposes.items())) or "(none yet)"


def _parse_type_and_tags(data: dict) -> tuple[str, list[str]]:
    doc_type = data.get("type") if data.get("type") in ("feature", "workflow") else "feature"
    tags = [t for t in data.get("tags", []) if isinstance(t, str)]
    return doc_type, tags


def classify_prompt(commit: Commit, existing: ExistingDocs) -> tuple[str, str]:
    """`(cacheable prefix, this commit's half)` — see the note above `CLASSIFY_COMMIT`.

    Split out so the batch path can build the same pair for many commits without going through
    `classify_change`, which answers one commit at a time.
    """
    return (
        CLASSIFY_PROMPT.format(
            existing_tags=existing.tags_line(), existing_docs=existing.docs_block()
        ),
        CLASSIFY_COMMIT.format(
            message=commit.message, diff=commit.diff[:DIFF_TRUNCATE_CHARS]
        ),
    )


def classify_change(
    repo_root: Path, commit: Commit, provider: Provider, existing: ExistingDocs | None = None
) -> Classification:
    existing = existing if existing is not None else ExistingDocs.load(repo_root)
    prefix, prompt = classify_prompt(commit, existing)
    raw = strip_code_fence(provider.generate(prompt, prefix=prefix, task="classify"))
    data = leading_json_object(raw)
    if data is None:
        return Classification(skip=True, reason=f"could not parse classification response: {raw!r}")

    if data.get("skip"):
        return Classification(skip=True, reason="model judged this change as non-feature-affecting")

    domain, topic = data.get("domain"), data.get("topic")
    if not domain or not topic:
        return Classification(skip=True, reason=f"classification missing domain/topic: {data!r}")
    doc_type, tags = _parse_type_and_tags(data)
    return Classification(
        skip=False, domain=domain, topic=topic, purpose=data.get("purpose", ""), doc_type=doc_type, tags=tags
    )


def _grounded_in_source(repo_root: Path, flag: str) -> bool:
    """Does this flag appear in tracked source outside `specs/`, on a line that isn't a comment?

    Docs legitimately name other tools' flags — `git diff --name-only`, `gh pr comment --body-file`
    — and those show up in the argument lists and usage examples that actually run them, so tracked
    source is the right second corpus after argparse.

    Comment lines are excluded for one specific reason: a comment explaining that a flag was
    considered and *rejected* would otherwise ground the exact mistake this check exists to catch.
    This repo has such a comment (above `export`'s mutually exclusive group in cli.py), and the doc
    that copied a flag out of it is why any of this is here. `specs/` is excluded too, or one doc's
    invention grounds the next one's.
    """
    hits = subprocess.run(
        ["git", "grep", "--fixed-strings", "-h", "-e", flag, "--", ":!specs/"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    ).stdout
    return any(not line.lstrip().startswith("#") for line in hits.splitlines())


def ungrounded_flags(repo_root: Path, body: str) -> list[str]:
    """Every `--flag` in this doc that neither the CLI accepts nor any real code mentions.

    The one error class in a generated doc that a machine can settle on its own. The model reads a
    diff, so a source comment naming a flag that doesn't exist reads to it exactly like one that
    does — and argparse already knows which is which, so this asks it rather than guessing.

    Deliberately not naming the flag that prompted this: writing a phantom flag into source is how
    the mistake propagated in the first place, and `_grounded_in_source` would then be one comment
    away from blessing it again.
    """
    from specky.cli import known_flags

    unknown = set(_FLAG.findall(body)) - known_flags()
    return sorted(flag for flag in unknown if not _grounded_in_source(repo_root, flag))


# A fenced ```mermaid block's source. Non-greedy, so a doc with two diagrams yields two matches.
_MERMAID_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)\n```", re.DOTALL)

# A node's class assignment (`A["Label"]:::feature`) and the `classDef` that gives it meaning. The
# name is optional in the first pattern on purpose — a bare `:::` with nothing after it is the
# defect this exists to catch, and it has to be matched before it can be removed.
_CLASS_SUFFIX = re.compile(r":::([A-Za-z_][A-Za-z0-9_-]*)?")
_CLASS_DEF = re.compile(r"^\s*classDef\s+([A-Za-z_][A-Za-z0-9_-]*)", re.MULTILINE)


def _quoted_spans(line: str) -> list[tuple[int, int]]:
    """Character ranges inside `"…"` on this line — where a `:::` is a label's text, not syntax."""
    spans, start = [], None
    for i, char in enumerate(line):
        if char != '"':
            continue
        if start is None:
            start = i
        else:
            spans.append((start, i))
            start = None
    return spans


def repair_mermaid(body: str) -> tuple[str, list[str]]:
    """Drop class suffixes a diagram can't honour. Returns the body and what was repaired.

    Repaired rather than refused, because that is what this codebase already does with a generated
    diagram: `catalog._mermaid_label` silently escapes the two characters that would end a node
    label early, instead of rejecting the title. The same logic applies here — the fix is
    unambiguous, and refusing a good document over one decorative token would be out of proportion.

    Two defects, and both are invisible at exactly the moment they matter:

    - **`A["Label"]:::`** — a class suffix with no class name. The vendored renderer parses it
      happily and the node simply comes out unstyled, so nothing anywhere reports a problem.
    - **`:::feature` with no `classDef feature`** — same outcome by a different route.

    Neither is worth a human's time to find, and a model writing a diagram by hand produces the
    first one often enough that it turned up on the first real run of `specky document`.

    A `:::` inside a quoted label is left alone: there it is somebody's text, not syntax.
    """
    repairs: list[str] = []

    def fix_block(match: re.Match) -> str:
        block = match.group(1)
        defined = set(_CLASS_DEF.findall(block))
        out_lines = []
        for line in block.splitlines():
            spans = _quoted_spans(line)

            def drop(suffix: re.Match) -> str:
                if any(start < suffix.start() < end for start, end in spans):
                    return suffix.group(0)  # inside a label, so it is text
                name = suffix.group(1)
                if name and name in defined:
                    return suffix.group(0)
                repairs.append(
                    f"`:::{name}` names no classDef" if name else "a `:::` with no class name"
                )
                return ""

            out_lines.append(_CLASS_SUFFIX.sub(drop, line))
        return "```mermaid\n" + "\n".join(out_lines) + "\n```"

    return _MERMAID_BLOCK.sub(fix_block, body), repairs


def unrenderable_mermaid(body: str) -> list[str]:
    """Every fenced mermaid block in this doc that the renderer cannot parse.

    Returns `[]` when the renderer isn't installed, which is not the same as "they are all fine" —
    it is "nothing can be said", and the caller treats it that way. `render_mermaid_svg` folds a
    missing Node, a missing install and a parse failure into one `None`, so the install is checked
    separately here or every diagram would look broken on a machine without the tool.

    This is the second half of the check and catches nothing the first half does: a diagram can
    parse perfectly and still be wrong (see `repair_mermaid`), and a diagram that does not parse is
    not something specky can fix on the author's behalf.

    It is a coarse net, deliberately described as one. Measured against the vendored renderer, it
    catches an unknown diagram type and prose that is not a diagram at all; it does *not* catch an
    unclosed bracket, a malformed arrow or a header with no body, all of which render into
    something. So the lint above is the precise half and this is the backstop, not the reverse.
    """
    from specky.mermaid_tool import tool_dir

    if tool_dir() is None:
        return []

    from specky.html_render import render_mermaid_svg

    broken = []
    for index, block in enumerate(_MERMAID_BLOCK.findall(body), 1):
        if not block.strip():
            broken.append(f"diagram {index} is empty")
        elif render_mermaid_svg(block) is None:
            first = next((l.strip() for l in block.splitlines() if l.strip()), "")
            broken.append(f"diagram {index} (`{first[:60]}`) does not parse")
    return broken


def lost_content(existing_body: str, new_body: str) -> str | None:
    """Why this regeneration reads as a rewrite rather than an update — or None if it looks like one.

    Two signals, and neither alone is enough. Both regressions in this repo's own history prove it:

    - `f087a01` dropped three whole `##` sections from `specs/cli/check.md` and kept 32% of its
      characters. A heading check catches it; so does a whole-doc ratio.
    - `eefb94b` kept every heading in `specs/cli/doctor.md` **and** 83% of its characters — while
      gutting `## How It Works` from 48 lines to 11, mermaid flowchart included. A grown Acceptance
      Tests table hid the loss in the total. Only a per-section ratio catches that one.

    So: a section may not vanish, and a section that survives may not lose most of its body. The
    whole-doc ratio stays as a backstop for a rewrite that restructures the headings entirely.
    """
    after = {title.lower(): "\n".join(lines) for title, lines in split_sections(new_body)}
    dropped: list[str] = []
    shrunk: list[tuple[float, str, int, int]] = []
    for title, lines in split_sections(existing_body):
        if not title:
            continue  # the preamble: frontmatter and the H1, rendered fresh either way
        kept = after.get(title.lower())
        # Both sides include their own `## ` line, which keeps the ratio honest for a short section.
        before = len("\n".join(lines).strip())
        if kept is None:
            dropped.append(title)
            continue
        now = len(kept.strip())
        if before >= MIN_SECTION_CHARS and now < before * MIN_KEPT_RATIO:
            shrunk.append((now / before, title, before, now))

    if dropped:
        listed = ", ".join(f"`## {title}`" for title in dropped[:3])
        more = f" and {len(dropped) - 3} more" if len(dropped) > 3 else ""
        also = f", and guts {len(shrunk)} other section(s)" if shrunk else ""
        return f"it drops {listed}{more}{also}"
    if shrunk:
        _, title, before, now = min(shrunk)
        also = f", as did {len(shrunk) - 1} other section(s)" if len(shrunk) > 1 else ""
        return f"`## {title}` keeps {round(100 * now / before)}% of its {before} characters{also}"
    before, now = len(existing_body.strip()), len(new_body.strip())
    if before and now < before * MIN_KEPT_RATIO:
        return f"the doc keeps {round(100 * now / before)}% of its {before} characters"
    return None


def doc_problem(repo_root: Path, existing_body: str | None, body: str) -> str | None:
    """Why this doc must not reach disk, or None if it may.

    Both write paths ask this — `sync_feature_doc` below and `document.write` — and they have to ask
    it the same way. The guards are independent, but their *order* is policy: `lost_content` runs
    first because losing somebody's prose is the worst outcome available and the cheapest to state,
    and the mermaid check runs last because it is the only one that shells out to Node. An order
    that lives in two places is an order that drifts, and the drift would be silent — each path
    would still refuse, just not the same things first, so the two would explain the same bad doc
    differently.

    Not included here: `repair_mermaid`, which callers run *before* this, because it rewrites the
    body that everything below is then measured against.
    """
    if existing_body:
        lost = lost_content(existing_body, body)
        if lost:
            return lost

    invented = ungrounded_flags(repo_root, body)
    if invented:
        named = ", ".join(f"`{flag}`" for flag in invented)
        return f"it names {named}, which nothing in this repo accepts"

    broken = unrenderable_mermaid(body)
    if broken:
        return "; ".join(broken)
    return None


def _strip_own_heading(title: str, text: str) -> str:
    """Drop a leading `## Title` the model included anyway — the splice adds it back itself."""
    lines = text.strip().splitlines()
    heading = lines[0].lstrip("#").strip().lower() if lines and lines[0].startswith("#") else None
    return "\n".join(lines[1:]).strip() if heading == title.strip().lower() else text.strip()


def merge_sections(existing_body: str, updates: dict) -> str:
    """Splice replacement sections into a doc, leaving every other section byte-identical.

    An update naming a heading the doc doesn't have is appended in the order the model sent it —
    that's how a new `## Outcomes` arrives on a doc that never had one.
    """
    replacements = {
        title.strip().lower(): text
        for title, text in updates.items()
        if isinstance(title, str) and isinstance(text, str)
    }
    out: list[str] = []
    for title, lines in split_sections(existing_body):
        replacement = replacements.pop(title.strip().lower(), None) if title else None
        if replacement is None:
            out += lines
        else:
            out += [lines[0], "", _strip_own_heading(title, replacement), ""]
    for title, text in updates.items():
        if isinstance(title, str) and title.strip().lower() in replacements:
            out += [f"## {title.strip()}", "", _strip_own_heading(title, text), ""]
    return "\n".join(out).rstrip("\n") + "\n"


def _commit_block(commit: Commit) -> str:
    return (
        f"---\nThis commit changed the feature:\n\nCommit message:\n{commit.message}\n\n"
        f"Diff (may be truncated):\n{commit.diff[:DIFF_TRUNCATE_CHARS]}\n---\n\n"
    )


# Sections a doc of this type owes, and what to say when one is missing. The update path only ever
# sees the sections a doc already has, so a doc predating a template change would never be asked to
# grow the new one — `merge_sections` appends an unknown heading perfectly well, nothing was telling
# the model it could.
#
# The guidance lives in the table rather than in the sentence below it. A second entry here would
# otherwise inherit a prompt hardcoded to say "workflow" and to describe Edge Cases' own columns,
# which is a lie the next person to extend this would have no reason to expect.
REQUIRED_SECTIONS: dict[str, dict[str, str]] = {
    "workflow": {
        "Edge Cases": (
            "a table of Situation | What happens | Why, covering every branch off the happy path "
            "— what is refused, what is skipped silently, what a partial run leaves behind. Move "
            "any such case currently sitting in another section into it."
        )
    }
}


def _missing_sections(doc_type: str, titles: list[str]) -> list[tuple[str, str]]:
    """`(heading, what to write there)` for each section this doc owes and does not have."""
    have = {title.strip().lower() for title in titles}
    return [
        (section, guidance)
        for section, guidance in REQUIRED_SECTIONS.get(doc_type, {}).items()
        if section.lower() not in have
    ]


def update_feature_doc(
    existing_content: str,
    commit: Commit,
    domain: str,
    topic: str,
    provider: Provider,
    doc_type: str = "",
) -> str:
    """An existing doc's new body, built by replacing only the sections the model names.

    Falls back to the whole-body path when the response isn't the JSON shape asked for — same
    graceful degradation as `classify_change`, and `lost_content` guards the result either way, so
    a provider that ignores the format is no worse off than before this existed.
    """
    titles = [title for title, _ in split_sections(existing_content) if title]
    prompt = (
        f"You maintain specs/{domain}/{topic}.md, the living reference doc for this "
        "feature/workflow.\n\nIts current content follows in full.\n\n"
        f"{existing_content}\n\n"
        + _commit_block(commit)
        + SECTION_UPDATE_INSTRUCTIONS.format(
            section_list="\n".join(f"- {title}" for title in titles) or "(no sections yet)"
        )
    )
    for section, guidance in _missing_sections(doc_type, titles):
        prompt += (
            f"\nThis doc is a {doc_type} and has no `## {section}`. Add it in this pass: "
            f"{guidance}\n"
        )
    # Frontmatter off first, so an echo of the block the doc it was shown starts with doesn't hide
    # the envelope behind it; the whole-body fallback below wants it gone either way.
    _, raw = frontmatter.parse(strip_code_fence(provider.generate(prompt, task="doc")))
    parsed = leading_json_object(raw)
    if parsed is not None and isinstance(parsed.get("sections"), dict):
        return merge_sections(existing_content, parsed["sections"])
    # Not the JSON contract: read it as a whole replacement body.
    return raw + "\n"


def generate_feature_doc(
    existing_content: str | None,
    commit: Commit,
    domain: str,
    topic: str,
    provider: Provider,
    doc_type: str = "",
) -> str:
    """This feature's doc body. A doc that already exists is updated section by section
    (`update_feature_doc`); one that doesn't is written whole, from this commit alone.

    `doc_type` picks the template (`doc_style`). It comes from this run's classification, which is
    resolved before either path is entered — unlike `document.py`, where the model decides the type
    mid-run and so has to be shown both shapes up front.
    """
    if existing_content:
        return update_feature_doc(existing_content, commit, domain, topic, provider, doc_type)

    prompt = (
        f"You maintain specs/{domain}/{topic}.md, the living reference doc for this feature/workflow.\n\n"
        "No existing doc yet — write one from scratch based on this change, staying grounded in "
        "what's actually shown below (don't invent behaviour the diff/message doesn't evidence).\n\n"
        + _commit_block(commit)
        + doc_style(
            doc_type,
            domain_title=domain.replace("-", " ").title(),
            topic_title=topic.replace("-", " ").title(),
        )
        + "\nOutput ONLY the final markdown content of the doc file, nothing else "
        "(no commentary, no code fences around it)."
    )
    # Strip any frontmatter block the model produced: `sync_feature_doc` renders the real one
    # from this run's classification, and a second block would land on top of it. It happens for
    # a concrete reason — a commit that adds or edits a doc under specs/ carries that doc's own
    # frontmatter in its diff, and the model copies what it sees there into its output.
    _, body = frontmatter.parse(strip_code_fence(provider.generate(prompt, task="doc")))
    return body + "\n"


def _index_section_key(heading: str) -> str:
    """A MODULES.md heading reduced to what identifies its domain: letters and digits, lowercased.

    Punctuation and spacing are the entire difference between the heading a human wrote and the one
    specky generates for the same domain — `## N-Way Match` and `## Nway Match` are one section, and
    treating them as two is what put a duplicate index section in a real repo.

    Deliberately not prefix or fuzzy matching: `docs` and `documents` are separate domains in this
    repo's own tree, and collapsing them would file one domain's docs under the other's heading.
    """
    return re.sub(r"[^a-z0-9]", "", heading.lower())


def update_modules_index(repo_root: Path, domain: str, doc_rel_path: str, purpose: str) -> None:
    """Best-effort MODULES.md sync: adds a row for doc_rel_path under a '## {Domain}' section,
    creating the section if needed.

    Two rules keep it from duplicating what a hand-written index already says, because this runs
    unattended on every commit and the file it edits is one a human also edits:

    - A heading is matched on letters and digits only (`_index_section_key`), so an existing
      `## N-Way Match` is reused for domain `nway-match` instead of gaining a `## Nway Match` twin.
    - A doc already linked *anywhere* in the file is left alone, whatever section it sits under.
      Scanning only the matched section's own table is what let that twin carry a second row for a
      doc the file already indexed.

    Known limitation: matching is still equality, just on the normalized form. A heading carrying
    extra words — `## Billing (legacy)` — is a different key and won't be matched, so a second
    section can still appear for it. That's the deliberate trade: the only cheap way to catch it is
    prefix matching, which merges domains that are genuinely distinct.
    """
    modules_path = paths.modules_index(repo_root)
    heading = f"## {domain.replace('-', ' ').title()}"
    link_target = f"({doc_rel_path})"

    if not modules_path.exists():
        modules_path.parent.mkdir(parents=True, exist_ok=True)
        modules_path.write_text("# Modules\n")

    text = modules_path.read_text()
    if link_target in text:
        return  # already indexed somewhere in this file, under whatever heading

    lines = text.splitlines()

    wanted = _index_section_key(heading)
    heading_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().startswith("## ") and _index_section_key(line.strip()) == wanted
        ),
        None,
    )

    if heading_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [heading, "", "| Doc | Purpose |", "|---|---|", f"| [{doc_rel_path}]{link_target} | {purpose} |"]
        modules_path.write_text("\n".join(lines) + "\n")
        return

    table_start = None
    table_end = heading_idx + 1
    for i in range(heading_idx + 1, len(lines)):
        if lines[i].startswith("|"):
            if table_start is None:
                table_start = i
            table_end = i + 1
        elif table_start is not None:
            break
        elif lines[i].startswith("## "):
            break

    if table_start is None:
        lines[heading_idx + 1 : heading_idx + 1] = ["", "| Doc | Purpose |", "|---|---|"]
        table_end = heading_idx + 4

    placeholder_idx = next(
        (i for i in range(table_start or table_end, table_end) if "_(none yet)_" in lines[i]), None
    )
    new_row = f"| [{doc_rel_path}]{link_target} | {purpose} |"
    if placeholder_idx is not None:
        lines[placeholder_idx] = new_row
    else:
        lines.insert(table_end, new_row)

    modules_path.write_text("\n".join(lines) + "\n")


@dataclass(frozen=True)
class DocSync:
    """What `sync_feature_doc` decided about one commit.

    `path` is the doc this commit belongs to whether or not anything was written — a frozen or
    refused doc still earns its commit link, which is what `specky commit-info` answers with and
    what `specky features` counts. `written` is False when the doc on disk was deliberately left
    as it is, so a caller doesn't count it as changed. `note` is the line to print, always set:
    library code here does no printing of its own.
    """

    path: Path
    written: bool
    note: str


# Terms one doc may contribute to the glossary. A doc proposing more than this has stopped naming
# shared vocabulary and started restating its own contents.
MAX_GLOSSARY_TERMS = 25


def append_glossary_rows(repo_root: Path, terms: list[dict]) -> tuple[Path | None, int]:
    """Add unseen terms to `specs/GLOSSARY.md`. Returns the file and how many rows landed.

    **Additive, never a rewrite.** Existing definitions win and existing prose is untouched — this
    is a file people hand-edit, and it is also the one specky file with a machine contract on the
    other side: `html_render.load_glossary` parses it back with
    `^\\|\\s*\\*\\*([^*|]+)\\*\\*\\s*\\|\\s*(.+?)\\s*\\|\\s*$` to drive the viewer's term
    auto-linking, which degrades silently to a no-op against any row that doesn't match. So the
    round trip is guaranteed by construction: the existing terms are read with `load_glossary`
    itself — the writer's exact inverse — and a term carrying a `|` or a `*` is dropped rather than
    escaped, because the regex's term group is `[^*|]+` and an escaped pipe fails it just the same.
    """
    from specky.html_render import load_glossary

    path = paths.glossary(repo_root)
    existing = load_glossary(repo_root)
    known = {term.lower() for term in existing}

    rows = []
    for raw in terms[:MAX_GLOSSARY_TERMS]:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term", "")).strip()
        definition = " ".join(str(raw.get("definition", "")).split())
        if not term or not definition or "|" in term or "*" in term:
            continue
        if term.lower() in known:
            continue  # hand-written definitions win
        known.add(term.lower())  # and a term proposed twice in one run lands once
        rows.append(f"| **{term}** | {definition.replace('|', '/')} |")

    if not rows:
        return None, 0
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {repo_root.name} — Glossary\n\n| Term | Definition |\n|---|---|\n")

    # Appended after the last table row, so prose below the table survives.
    lines = path.read_text().splitlines()
    last_row = max((i for i, line in enumerate(lines) if line.startswith("|")), default=len(lines) - 1)
    lines[last_row + 1 : last_row + 1] = rows
    path.write_text("\n".join(lines).rstrip("\n") + "\n")
    return path, len(rows)


def stage_pending(repo_root: Path, domain: str, topic: str, content: str) -> Path:
    """Park a refused draft where a human can read it, out of git's reach (see `PENDING_DIR`)."""
    path = repo_root / PENDING_DIR / domain / f"{topic}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def sync_feature_doc(
    repo_root: Path, commit: Commit, provider: Provider, existing: ExistingDocs | None = None
) -> DocSync | None:
    """Classify a commit and, if it affects a feature/workflow, generate or update its reference
    doc under specs/<domain>/<topic>.md. Returns what it did, or None if the commit was skipped.

    A regeneration is not trusted blindly. Three things can stop it reaching disk, and all three
    exist because they had to: the hook runs on every commit, so anything it gets wrong it gets
    wrong repeatedly, and it silently outvotes whoever corrected the doc by hand last time.

    1. `authored: human` in the frontmatter freezes the body outright — no generation call at all.
    2. `lost_content` refuses a rewrite that drops or guts a section.
    3. `ungrounded_flags` refuses a doc naming a `--flag` that nothing in this repo accepts.

    In cases 2 and 3 the draft goes to `.specky/pending/` rather than being thrown away: it may
    well be a better doc than what's on disk, and that's a judgement for a human. `specky doctor`
    warns while one is waiting there.

    `existing` is the shared snapshot when a caller is walking many commits (see `sync()`);
    left out, it's loaded for this one commit."""
    existing = existing if existing is not None else ExistingDocs.load(repo_root)
    classification = classify_change(repo_root, commit, provider, existing)
    if classification.skip:
        return None

    doc_path = paths.docs_root(repo_root) / classification.domain / f"{classification.topic}.md"
    rel = doc_path.relative_to(repo_root)
    existing_meta: dict = {}
    existing_body = None
    if doc_path.exists():
        existing_meta, existing_body = frontmatter.parse(doc_path.read_text())

    # Checked before generating, so a frozen doc costs nothing but the classification call that
    # identified it. Still linked and still reported: a commit that changed this feature is
    # exactly when someone should look at the doc they took ownership of.
    if str(existing_meta.get("authored", "")).strip().lower() == "human":
        return DocSync(doc_path, False, f"left {rel} alone (authored: human) — may need a look")

    body = generate_feature_doc(
        existing_body,
        commit,
        classification.domain,
        classification.topic,
        provider,
        classification.doc_type,
    )
    # Same two checks `document.write` applies, for the same reason: this path rewrites whole
    # sections, and a section carrying a diagram can come back with it mangled.
    body, _ = repair_mermaid(body)

    # type/tags come from this run's classification; hand-authored `related`, `owner`, `authored`
    # and `origin` values are preserved across regenerations since the AI is never asked to produce
    # any of them. Losing an `owner:` to an automatic doc update would be worse than never having
    # supported it — the hook runs on every commit, so it would silently strip the line within a
    # day of someone adding it. `origin` is `specky adopt`'s pointer back to where a doc used to
    # live, which is the one thing a reader needs to resolve a stale link somebody else wrote.
    # `sources` joins them for a different reason than the rest: it isn't hand-written, it's what
    # `document.write` recorded about which files a doc was written from, and it's the only thing
    # giving such a doc `specky check` coverage. The AI is never asked for it here, so without this
    # line the first commit-driven update of a doc written by `specky document` would silently drop
    # that coverage — within a day, since the hook runs on every commit.
    meta: dict[str, str | list[str]] = {"type": classification.doc_type, "tags": classification.tags}
    for key in ("related", "owner", "authored", "origin", "sources"):
        if existing_meta.get(key):
            meta[key] = existing_meta[key]
    content = frontmatter.render(meta, body)

    problem = doc_problem(repo_root, existing_body, body)
    if problem:
        pending = stage_pending(repo_root, classification.domain, classification.topic, content)
        return DocSync(
            doc_path,
            False,
            f"refused to rewrite {rel} — {problem}. Draft kept at "
            f"{pending.relative_to(repo_root)}",
        )

    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(content)

    doc_rel_path = f"{classification.domain}/{classification.topic}.md"
    update_modules_index(repo_root, classification.domain, doc_rel_path, classification.purpose)
    existing.record(
        f"{classification.domain}/{classification.topic}", classification.purpose, classification.tags
    )
    return DocSync(doc_path, True, f"updated {rel}")


TAG_PROMPT = """You maintain a set of feature/workflow reference docs under specs/<domain>/<topic>.md for \
this codebase. Classify the following existing doc.

Respond with ONLY a JSON object, no other text:
{{"type": "feature|workflow", "tags": ["tag1", "tag2"]}}

""" + TAG_GUIDANCE + """

Doc title: {title}

Doc content:
{content}
"""


def backfill_tags(repo_root: Path, provider: Provider) -> list[Path]:
    """Add type/tags frontmatter to feature/workflow docs written before this feature existed.
    Idempotent — skips any doc that already has a `type` set."""
    specs_root = paths.docs_root(repo_root)
    if not specs_root.exists():
        return []

    # One walk for the whole run; each doc we tag folds its own tags back in via record(), so a
    # doc tagged early still steers the docs tagged after it toward the same vocabulary.
    existing = ExistingDocs.load(repo_root)

    updated: list[Path] = []
    for md_path in sorted(specs_root.rglob("*.md")):
        rel = md_path.relative_to(specs_root)
        domain = rel.parts[0] if len(rel.parts) > 1 else "root"
        if domain in _UNTAGGED_DOMAINS:
            continue

        meta, body = frontmatter.parse(md_path.read_text())
        if meta.get("type"):
            continue

        title = _doc_title(body, md_path.stem)
        prompt = TAG_PROMPT.format(
            title=title, content=body[:DIFF_TRUNCATE_CHARS], existing_tags=existing.tags_line()
        )
        data = leading_json_object(strip_code_fence(provider.generate(prompt, task="tag")))
        if data is None:
            continue

        doc_type, tags = _parse_type_and_tags(data)
        meta.update({"type": doc_type, "tags": tags})
        md_path.write_text(frontmatter.render(meta, body))
        existing.record(rel.with_suffix("").as_posix(), title, tags)
        updated.append(md_path)
    return updated
