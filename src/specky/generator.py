"""Keeps specs/<domain>/<topic>.md in sync with what a feature/workflow currently does.

This is the automatic counterpart of the `document-domain` skill: where the skill is
agent-driven (a human asks an agent to document a domain, the agent reasons over the
codebase), this module runs unattended off the configured AI provider, triggered per
commit by commit_doc.py. It classifies whether a commit's diff affects a documented
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

from specky import frontmatter
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
""" + TAG_GUIDANCE + """

Commit message:
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


def _strip_code_fence(text: str) -> str:
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text.strip(), re.DOTALL)
    return match.group(1) if match else text.strip()


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
        specs_root = repo_root / "specs"
        if not specs_root.exists():
            return snapshot

        purposes = modules_purposes(specs_root / "MODULES.md")
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


def classify_change(
    repo_root: Path, commit: Commit, provider: Provider, existing: ExistingDocs | None = None
) -> Classification:
    existing = existing if existing is not None else ExistingDocs.load(repo_root)
    prompt = CLASSIFY_PROMPT.format(
        message=commit.message,
        diff=commit.diff[:DIFF_TRUNCATE_CHARS],
        existing_tags=existing.tags_line(),
        existing_docs=existing.docs_block(),
    )
    raw = _strip_code_fence(provider.generate(prompt))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
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


def update_feature_doc(
    existing_content: str, commit: Commit, domain: str, topic: str, provider: Provider
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
    raw = _strip_code_fence(provider.generate(prompt))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("sections"), dict):
        return merge_sections(existing_content, parsed["sections"])
    # Not the JSON contract: read it as a whole replacement body, frontmatter echo and all.
    _, body = frontmatter.parse(raw)
    return body + "\n"


def generate_feature_doc(
    existing_content: str | None, commit: Commit, domain: str, topic: str, provider: Provider
) -> str:
    """This feature's doc body. A doc that already exists is updated section by section
    (`update_feature_doc`); one that doesn't is written whole, from this commit alone."""
    if existing_content:
        return update_feature_doc(existing_content, commit, domain, topic, provider)

    prompt = (
        f"You maintain specs/{domain}/{topic}.md, the living reference doc for this feature/workflow.\n\n"
        "No existing doc yet — write one from scratch based on this change, staying grounded in "
        "what's actually shown below (don't invent behaviour the diff/message doesn't evidence).\n\n"
        + _commit_block(commit)
        + DOC_STYLE_INSTRUCTIONS.format(
            domain_title=domain.replace("-", " ").title(), topic_title=topic.replace("-", " ").title()
        )
        + "\nOutput ONLY the final markdown content of the doc file, nothing else "
        "(no commentary, no code fences around it)."
    )
    # Strip any frontmatter block the model produced: `sync_feature_doc` renders the real one
    # from this run's classification, and a second block would land on top of it. It happens for
    # a concrete reason — a commit that adds or edits a doc under specs/ carries that doc's own
    # frontmatter in its diff, and the model copies what it sees there into its output.
    _, body = frontmatter.parse(_strip_code_fence(provider.generate(prompt)))
    return body + "\n"


def update_modules_index(repo_root: Path, domain: str, doc_rel_path: str, purpose: str) -> None:
    """Best-effort MODULES.md sync: adds a row for doc_rel_path under a '## {Domain}' section,
    creating the section if needed. Known limitation: only recognizes a section whose heading is
    exactly '## {Domain Title}' — a hand-written heading with extra text won't be matched, so this
    may create a duplicate section rather than reusing it."""
    modules_path = repo_root / "specs" / "MODULES.md"
    heading = f"## {domain.replace('-', ' ').title()}"
    link_target = f"({doc_rel_path})"

    if not modules_path.exists():
        modules_path.parent.mkdir(parents=True, exist_ok=True)
        modules_path.write_text("# Modules\n")

    lines = modules_path.read_text().splitlines()

    heading_idx = next((i for i, line in enumerate(lines) if line.strip() == heading), None)

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

    for i in range(table_start or table_end, table_end):
        if link_target in lines[i]:
            return  # already indexed, nothing to do

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


def _stage_pending(repo_root: Path, domain: str, topic: str, content: str) -> Path:
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

    doc_path = repo_root / "specs" / classification.domain / f"{classification.topic}.md"
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

    body = generate_feature_doc(existing_body, commit, classification.domain, classification.topic, provider)

    # type/tags come from this run's classification; hand-authored `related`, `owner` and
    # `authored` values are preserved across regenerations since the AI is never asked to produce
    # any of them. Losing an `owner:` to an automatic doc update would be worse than never having
    # supported it — the hook runs on every commit, so it would silently strip the line within a
    # day of someone adding it.
    meta: dict[str, str | list[str]] = {"type": classification.doc_type, "tags": classification.tags}
    for key in ("related", "owner", "authored"):
        if existing_meta.get(key):
            meta[key] = existing_meta[key]
    content = frontmatter.render(meta, body)

    problem = lost_content(existing_body, body) if existing_body else None
    if not problem:
        invented = ungrounded_flags(repo_root, body)
        if invented:
            named = ", ".join(f"`{flag}`" for flag in invented)
            problem = f"it names {named}, which nothing in this repo accepts"
    if problem:
        pending = _stage_pending(repo_root, classification.domain, classification.topic, content)
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
    specs_root = repo_root / "specs"
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
        raw = _strip_code_fence(provider.generate(prompt))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        doc_type, tags = _parse_type_and_tags(data)
        meta.update({"type": doc_type, "tags": tags})
        md_path.write_text(frontmatter.render(meta, body))
        existing.record(rel.with_suffix("").as_posix(), title, tags)
        updated.append(md_path)
    return updated
