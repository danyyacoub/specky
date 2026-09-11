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
from dataclasses import dataclass, field
from pathlib import Path

from specky import frontmatter
from specky.ai_provider import Provider
from specky.commit_doc import DIFF_TRUNCATE_CHARS, Commit

# Docs excluded from type/tags classification — root-level meta docs and the per-commit
# changelog trail aren't feature/workflow reference docs.
_UNTAGGED_DOMAINS = {"root", "history"}

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


def _existing_tags(repo_root: Path) -> list[str]:
    """Tags already in use across feature/workflow docs on disk, so classification prompts
    can be steered toward reusing them instead of inventing near-duplicates."""
    specs_root = repo_root / "specs"
    if not specs_root.exists():
        return []

    tags: set[str] = set()
    for md_path in specs_root.rglob("*.md"):
        rel = md_path.relative_to(specs_root)
        domain = rel.parts[0] if len(rel.parts) > 1 else "root"
        if domain in _UNTAGGED_DOMAINS:
            continue
        meta, _ = frontmatter.parse(md_path.read_text())
        tags.update(meta.get("tags", []))
    return sorted(tags)


def _parse_type_and_tags(data: dict) -> tuple[str, list[str]]:
    doc_type = data.get("type") if data.get("type") in ("feature", "workflow") else "feature"
    tags = [t for t in data.get("tags", []) if isinstance(t, str)]
    return doc_type, tags


def classify_change(repo_root: Path, commit: Commit, provider: Provider) -> Classification:
    existing_tags = ", ".join(_existing_tags(repo_root)) or "(none yet)"
    prompt = CLASSIFY_PROMPT.format(
        message=commit.message, diff=commit.diff[:DIFF_TRUNCATE_CHARS], existing_tags=existing_tags
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


def generate_feature_doc(
    existing_content: str | None, commit: Commit, domain: str, topic: str, provider: Provider
) -> str:
    if existing_content:
        context = (
            "Existing doc content follows — update only the parts this change affects, "
            f"preserve everything else that's still accurate:\n\n{existing_content}"
        )
    else:
        context = (
            "No existing doc yet — write one from scratch based on this change, staying grounded in "
            "what's actually shown below (don't invent behaviour the diff/message doesn't evidence)."
        )

    prompt = (
        f"You maintain specs/{domain}/{topic}.md, the living reference doc for this feature/workflow.\n\n"
        f"{context}\n\n---\nThis commit changed the feature:\n\n"
        f"Commit message:\n{commit.message}\n\nDiff (may be truncated):\n{commit.diff[:DIFF_TRUNCATE_CHARS]}\n---\n\n"
        + DOC_STYLE_INSTRUCTIONS.format(
            domain_title=domain.replace("-", " ").title(), topic_title=topic.replace("-", " ").title()
        )
        + "\nOutput ONLY the final markdown content of the doc file, nothing else "
        "(no commentary, no code fences around it)."
    )
    return _strip_code_fence(provider.generate(prompt)) + "\n"


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


def sync_feature_doc(repo_root: Path, commit: Commit, provider: Provider) -> Path | None:
    """Classify a commit and, if it affects a feature/workflow, generate or update its reference
    doc under specs/<domain>/<topic>.md. Returns the doc path written, or None if skipped."""
    classification = classify_change(repo_root, commit, provider)
    if classification.skip:
        return None

    doc_path = repo_root / "specs" / classification.domain / f"{classification.topic}.md"
    existing_meta: dict = {}
    existing_body = None
    if doc_path.exists():
        existing_meta, existing_body = frontmatter.parse(doc_path.read_text())

    body = generate_feature_doc(existing_body, commit, classification.domain, classification.topic, provider)

    # type/tags come from this run's classification; a hand-authored `related` list is
    # preserved across regenerations since the AI is never asked to produce one.
    meta: dict[str, str | list[str]] = {"type": classification.doc_type, "tags": classification.tags}
    if existing_meta.get("related"):
        meta["related"] = existing_meta["related"]
    content = frontmatter.render(meta, body)

    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(content)

    doc_rel_path = f"{classification.domain}/{classification.topic}.md"
    update_modules_index(repo_root, classification.domain, doc_rel_path, classification.purpose)
    return doc_path


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

    updated: list[Path] = []
    for md_path in sorted(specs_root.rglob("*.md")):
        rel = md_path.relative_to(specs_root)
        domain = rel.parts[0] if len(rel.parts) > 1 else "root"
        if domain in _UNTAGGED_DOMAINS:
            continue

        meta, body = frontmatter.parse(md_path.read_text())
        if meta.get("type"):
            continue

        title = next(
            (line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")), md_path.stem
        )
        existing_tags = ", ".join(_existing_tags(repo_root)) or "(none yet)"
        prompt = TAG_PROMPT.format(title=title, content=body[:DIFF_TRUNCATE_CHARS], existing_tags=existing_tags)
        raw = _strip_code_fence(provider.generate(prompt))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        doc_type, tags = _parse_type_and_tags(data)
        meta.update({"type": doc_type, "tags": tags})
        md_path.write_text(frontmatter.render(meta, body))
        updated.append(md_path)
    return updated
