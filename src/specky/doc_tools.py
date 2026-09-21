"""What the Spec Assistant may look at while it drafts a spec: the docs tree and the git history.

`tools.py` is the toolbox for `specky document`, which writes a doc *from code*. This one is for a
reader proposing a change in the viewer's Spec Assistant, and it deliberately reads no code at all:
the question there is "where does this belong, and what does it change about what the docs already
promise", and the docs plus the history of why they say it are the whole answer. It is also what
keeps a draft cheap enough to run from a browser panel.

Two layers, because two kinds of caller use them:

- **Plain functions** (`list_domains`, `search_docs`, `read_doc`, `doc_behaviours`, `doc_history`,
  `search_history`) return data. `mcp_server.py` exposes them as MCP tools — `catalog`'s own
  `commits_for_doc` standing in for `doc_history` — so Claude Code or opencode can run the same
  drafting workflow with their own model.
- **`DocToolbox`** hands them to specky's own model inside `Provider.converse`, as text, with a
  character budget and a call log — and adds the *terminal* tools a stage ends with
  (`set_scope`, `ask_user`, `submit_impact`). Those raise `StageDone`, the way `tools.submit_doc`
  raises `DocSubmitted`: the tool that ends a run unwinds straight out of the provider's loop.

Everything a terminal tool accepts is validated here, whichever way it arrived — a tool call, or the
JSON envelope a provider with no tool channel sends back (`spec_draft._degraded`). The same checks
on both paths is the point of routing the envelope through `invoke`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from specky import catalog, frontmatter, paths, source
from specky.db import connect, fts_match_query
from specky.generator import modules_purposes
from specky.testgen import PLACEHOLDERS, section, tables
from specky.tools import Tool

# Characters of tool output one stage may consume. The same figure as the chat panel's context
# budget (`chat_server.CONTEXT_CHARS_MAX`): a stage reads docs, and a question's worth of docs is
# what that budget was sized for. A runaway guard, not a working limit.
DRAFT_TOOL_BUDGET = 60_000

# One doc's share, as in the chat panel (`chat_server.DOC_CHARS_MAX`).
READ_DOC_CHARS = 24_000

SEARCH_LIMIT_DEFAULT = 8
SEARCH_LIMIT_MAX = 25
HISTORY_LIMIT = 10
# A commit's micro-doc summary, as the panel sends it (`chat_server.COMMIT_CHARS_MAX`).
COMMIT_SUMMARY_CHARS = 2_000

# Caps on what a terminal tool will carry. Each of these is replayed into every later prompt of the
# draft *and* round-trips through the reader's browser, so none of them can be left open.
MAX_OPTIONS = 6
MAX_CHANGES = 20
MAX_BEHAVIOURS = 30
TEXT_CHARS = 400

DOC_TYPES = ("feature", "workflow")
CHANGE_KINDS = ("add", "modify", "remove")
BEHAVIOUR_KINDS = ("changed", "new", "removed")

# The words a question is made of rather than the words it is about. Stripped before ranking, and
# only here: `fts_match_query()` is shared with `specky search`, where the reader typed the terms
# deliberately. A question doesn't work that way — "how is payment implemented" ORs to five terms,
# three of which are in every doc in the repo, so bm25 scored 30 docs within a rounding error of
# each other and the top five came back as MODULES.md, GLOSSARY.md and PRODUCT.md while the doc
# named payment-settlement-and-renewal.md ranked ninth. Public because `spec_draft` strips the same
# words when it names a new topic from a request.
QUESTION_WORDS = frozenset(
    """a about an and any are as at be been but by can did do does for from get had has have how i
    if in into is it its me my no not of on or our should so than that the their them then there
    these they this to use used was we what when where which who why will with would you
    your""".split()
)

# Column order in documents_fts is (path, domain, title, content, tags). A term in the file name or
# the heading is the reader having named the topic; the same term buried in a long body is often
# just a cross-reference. Weighting the short columns up is what pulls a domain's own doc above the
# index pages that mention every term in the repo once.
DOC_RANK = "bm25(documents_fts, 8.0, 2.0, 8.0, 1.0, 4.0)"

# The sections of a doc that state behaviour, and the prefix each one's row ids carry. A row id is
# how the impact stage points at the exact promise a change breaks ("AT-3 no longer holds"), and how
# the acceptance stage says which row a test pins down — far less ambiguous than quoting the row.
_BEHAVIOUR_SECTIONS = (
    ("how it works", "STEP"),
    ("outcomes", "OUT"),
    ("edge cases", "EDGE"),
    ("acceptance tests", "AT"),
)
_NUMBERED_STEP = re.compile(r"^\s*\d+\.\s+(.*\S)")
_KEBAB = re.compile(r"[^a-z0-9]+")


def topic_match(question: str) -> str | None:
    """The FTS5 expression for a question: the words naming a topic, not the ones asking about it.
    Falls back to the whole question when stripping leaves nothing ("how does this work")."""
    topical = " ".join(
        word for word in re.findall(r"\w+", question) if word.lower() not in QUESTION_WORDS
    )
    return fts_match_query(topical) or fts_match_query(question)


def kebab(text: str) -> str:
    """`"Retry Logic"` → `"retry-logic"`: the only shape a domain or topic may take on disk."""
    return _KEBAB.sub("-", str(text).lower()).strip("-")


def clip(text: Any, cap: int = TEXT_CHARS) -> str:
    """One field a model or a browser sent, as a single bounded line: whitespace collapsed, `None`
    as `""`, anything past `cap` cut with a bare `…`. Not `source.clip`, which keeps a document's
    lines and announces its cut — this is for short fields, where a notice would outweigh them."""
    text = " ".join(str(text).split()) if text is not None else ""
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def _is_history(repo_root: Path, doc_path: str) -> bool:
    return doc_path.startswith(paths.history_prefix(repo_root))


def _limit(value: Any, default: int = SEARCH_LIMIT_DEFAULT) -> int:
    """A result count a caller sent, clamped — or the default when it isn't a number at all, the
    way `tools.Toolbox.search_code` treats one, rather than an error over an argument."""
    try:
        return max(1, min(int(value), SEARCH_LIMIT_MAX))
    except (TypeError, ValueError):
        return default


def resolve_doc(repo_root: Path, path: Any) -> tuple[Path, str]:
    """A doc path as `(file, repo-relative path)`, or `ValueError` saying why it isn't one.

    Either spelling is accepted (`specs/chat/foo.md`, `chat/foo.md`) and the one returned is always
    the repo-relative form a source is cited by. The file must exist and sit inside the docs tree
    (`paths.doc_in_tree`); the caller turns the error into whatever its surface reports.
    """
    rel_path = str(path or "").strip().lstrip("./")
    if not rel_path:
        raise ValueError("a doc path is needed")
    candidate = paths.doc_in_tree(repo_root, rel_path)
    if candidate is None:
        raise ValueError(f"{rel_path} is not under {paths.docs_root(repo_root).name}/")
    if not candidate.is_file():
        raise ValueError(f"no doc at {rel_path}")
    return candidate, candidate.resolve().relative_to(repo_root.resolve()).as_posix()


def behaviours_text(rows: list[dict]) -> str:
    """`doc_behaviours` rows as a model reads them, one `ID  text` line each. The one spelling, for
    the tool result and the stage prompts alike: the model quotes these ids back as `ref`."""
    return "\n".join(f"{row['id']}  {row['text']}" for row in rows)


# --- the plain functions: data in, data out -------------------------------------------------------


def list_domains(repo_root: Path) -> list[dict]:
    """Every domain in the docs tree, with the docs in it.

    Read off disk rather than from the index, for the reason `generator.ExistingDocs` gives: the
    index is routinely a commit behind, and "does a doc for this already exist" is exactly the
    question a stale answer gets wrong.
    """
    root = paths.docs_root(repo_root)
    if not root.is_dir():
        return []
    purposes = modules_purposes(paths.modules_index(repo_root))
    domains: dict[str, list[dict]] = {}
    for md_path in sorted(root.rglob("*.md")):
        rel = md_path.relative_to(root)
        if len(rel.parts) < 2 or rel.parts[0] == paths.HISTORY_DIR_NAME:
            continue  # root-level index docs, and one-per-commit history
        meta, body = frontmatter.parse(md_path.read_text())
        title = next(
            (line.lstrip("#").strip() for line in body.splitlines() if line.startswith("# ")),
            md_path.stem,
        )
        domains.setdefault(rel.parts[0], []).append(
            {
                "path": md_path.relative_to(repo_root).as_posix(),
                "title": title,
                "type": str(meta.get("type", "")),
                "purpose": purposes.get(rel.as_posix(), ""),
            }
        )
    return [{"domain": name, "docs": docs} for name, docs in sorted(domains.items())]


def domains_text(repo_root: Path) -> str:
    """`list_domains` as the text a model reads: one block per domain, one line per doc."""
    blocks = []
    for entry in list_domains(repo_root):
        lines = [f"{entry['domain']}/"]
        for doc in entry["docs"]:
            kind = f" [{doc['type']}]" if doc["type"] else ""
            purpose = f" — {doc['purpose']}" if doc["purpose"] else ""
            lines.append(f"  - {doc['path']}{kind}: {doc['title']}{purpose}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks) or "The docs tree is empty — every domain is new."


def search_docs(
    repo_root: Path, query: str, domain: str | None = None, limit: int = SEARCH_LIMIT_DEFAULT
) -> list[dict]:
    """Docs ranked for `query`, the way the panel ranks them — history docs left out, since a
    commit's micro-doc is what `search_history` is for."""
    match = topic_match(query)
    if match is None:
        return []
    limit = _limit(limit)
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, snippet(documents_fts, 3, '', '', '…', 24) "
            f"FROM documents_fts WHERE documents_fts MATCH ? ORDER BY {DOC_RANK} LIMIT 60",
            (match,),
        ).fetchall()
    finally:
        conn.close()
    hits = [
        {"path": path, "domain": dom, "title": title, "snippet": snippet}
        for path, dom, title, snippet in rows
        if not _is_history(repo_root, path) and (not domain or dom == domain)
    ]
    return hits[:limit]


def read_doc(repo_root: Path, path: str, max_chars: int = READ_DOC_CHARS) -> str:
    """One doc's full text, frontmatter included, cut past `max_chars` with the cut declared.
    Raises `ValueError` as `resolve_doc` does."""
    doc, _ = resolve_doc(repo_root, path)
    return source.clip(doc.read_text(), max_chars)


def doc_behaviours(repo_root: Path, path: str) -> list[dict]:
    """Every behaviour a doc states, one row each, with a stable id.

    The numbered steps of How It Works (`STEP-n`), and the rows of its Outcomes (`OUT-n`), Edge
    Cases (`EDGE-n`) and Acceptance Tests (`AT-n`) tables. Each row is `{id, section, text,
    fields}`: `fields` maps a table's header to that row's cell, `text` is the same row flattened
    for a reader. Placeholder rows ("n/a", "TBD") are not behaviours and get no id.
    """
    doc, _ = resolve_doc(repo_root, path)
    _, body = frontmatter.parse(doc.read_text())
    rows: list[dict] = []
    for title, prefix in _BEHAVIOUR_SECTIONS:
        lines = section(body, title)
        label = title.title()
        n = 0
        if prefix == "STEP":
            for line in lines:
                step = _NUMBERED_STEP.match(line)
                if step:
                    n += 1
                    text = step.group(1).replace("**", "")
                    rows.append({"id": f"STEP-{n}", "section": label, "text": text, "fields": {}})
            continue
        for header, data in tables(lines):
            for cells in data:
                if all(cell.strip().lower() in PLACEHOLDERS for cell in cells):
                    continue
                n += 1
                fields = {h: c for h, c in zip(header, cells)}
                text = " · ".join(f"{h}: {c}" for h, c in fields.items() if c)
                rows.append({"id": f"{prefix}-{n}", "section": label, "text": text, "fields": fields})
    return rows


def doc_history(repo_root: Path, path: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """Commits linked to one doc, most recent first (`catalog.commits_for_doc`, capped). Raises
    `ValueError` as `resolve_doc` does — the index keys commits by the repo-relative path."""
    _, rel_path = resolve_doc(repo_root, path)
    return catalog.commits_for_doc(repo_root, rel_path)[:limit]


def search_history(repo_root: Path, query: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """Commits whose message or micro-doc summary matches `query` — why the docs say what they
    say, and what used to be true before they did."""
    match = topic_match(query)
    if match is None:
        return []
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT sha, message, summary FROM commits_fts WHERE commits_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, _limit(limit, HISTORY_LIMIT)),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"sha": sha[:8], "message": message, "summary": clip(summary, COMMIT_SUMMARY_CHARS)}
        for sha, message, summary in rows
    ]


# --- the terminal tools' payloads -----------------------------------------------------------------


class StageDone(Exception):
    """A terminal tool was called — the stage is over, with `kind` and a validated `payload`.

    `kind` is `"scope"`, `"ask"` or `"impact"`. An exception for the reason `tools.DocSubmitted` is
    one: the call arrives several layers inside a provider's loop, and unwinding says "finished"
    exactly once.
    """

    def __init__(self, kind: str, payload: dict) -> None:
        super().__init__(f"stage done: {kind}")
        self.kind = kind
        self.payload = payload


def scope_payload(
    repo_root: Path,
    domain: Any,
    topic: Any = "",
    doc_type: Any = "feature",
    existing_doc: Any = None,
    reason: Any = "",
) -> dict | str:
    """A scope as `{domain, topic, type, existing, path, reason}`, or the reason it isn't one.

    An existing doc outranks whatever domain/topic came with it: the path is the fact, and a model
    that names `specs/chat/foo.md` and domain `search` has told us where the doc is, not where it
    ought to move. Everything is rebuilt from parts here — a `path` is never taken from the caller.
    """
    existing = str(existing_doc or "").strip() or None
    meta: dict = {}
    if existing:
        try:
            doc, existing = resolve_doc(repo_root, existing)
        except ValueError:
            return (
                f"No doc at {existing}. Pass existing_doc only for a doc that exists — one listed "
                "in the map of existing docs — or leave it out for a new doc."
            )
        rel = doc.resolve().relative_to(paths.docs_root(repo_root).resolve())
        if len(rel.parts) != 2 or rel.parts[0] == paths.HISTORY_DIR_NAME:
            return f"{existing} is not a <domain>/<topic>.md doc."
        domain, topic = rel.parts[0], rel.stem
        meta = frontmatter.parse(doc.read_text())[0]
    domain, topic = kebab(domain), kebab(topic)
    if not domain or not topic:
        return "A scope needs both a domain and a topic, in kebab-case."
    if topic == "readme":
        return "`readme` is never a topic — name what the doc is about."
    kind = doc_type if doc_type in DOC_TYPES else meta.get("type", "feature")
    return {
        "domain": domain,
        "topic": topic,
        "type": kind if kind in DOC_TYPES else "feature",
        "existing": existing,
        "path": f"{paths.docs_prefix(repo_root)}{domain}/{topic}.md",
        "reason": clip(reason),
    }


def question_payload(question: Any, options: Any = None) -> dict | str:
    """A question for the reader as `{text, options: [{label, domain?, topic?, type?,
    existing_doc?}]}` — an option that names a place is answered in one click, without the model."""
    text = clip(question)
    if not text:
        return "ask_user needs a question."
    out = []
    for raw in options if isinstance(options, list) else []:
        if isinstance(raw, str):
            raw = {"label": raw}
        if not isinstance(raw, dict) or not clip(raw.get("label"), 120):
            continue
        option = {"label": clip(raw.get("label"), 120)}
        for key in ("domain", "topic", "type", "existing_doc"):
            if raw.get(key):
                option[key] = clip(raw[key], 200)
        out.append(option)
    return {"text": text, "options": out[:MAX_OPTIONS]}


def impact_payload(summary: Any = "", changes: Any = None, behaviours: Any = None) -> dict | str:
    """What a change does to the docs, as `{summary, changes, behaviours}`.

    A kind the model invented is coerced to the nearest honest one rather than refused: "update" is
    a `modify`, and an unknown behaviour kind is a `changed`, which is the one that asks a reader to
    look at it.
    """
    rows = []
    for raw in changes if isinstance(changes, list) else []:
        if not isinstance(raw, dict) or not clip(raw.get("summary")):
            continue
        kind = str(raw.get("kind", "")).lower()
        if kind in ("new", "added"):
            kind = "add"
        elif kind not in CHANGE_KINDS:
            kind = "modify"
        rows.append(
            {"section": clip(raw.get("section"), 80), "kind": kind, "summary": clip(raw["summary"])}
        )
    found = []
    for raw in behaviours if isinstance(behaviours, list) else []:
        if not isinstance(raw, dict) or not clip(raw.get("behaviour")):
            continue
        kind = str(raw.get("kind", "")).lower()
        found.append(
            {
                "ref": clip(raw.get("ref") or "", 80) or None,
                "behaviour": clip(raw["behaviour"], 200),
                "today": clip(raw.get("today")),
                "after": clip(raw.get("after")),
                "kind": kind if kind in BEHAVIOUR_KINDS else "changed",
            }
        )
    if not rows and not found:
        return (
            "submit_impact needs at least one change or behaviour. If this request changes nothing "
            "the docs promise, ask_user what the reader expects to be different."
        )
    return {
        "summary": clip(summary, 600),
        "changes": rows[:MAX_CHANGES],
        "behaviours": found[:MAX_BEHAVIOURS],
    }


# --- the toolbox handed to specky's own model -----------------------------------------------------

# No `list_domains` tool here, unlike the MCP server: specky's own model gets the map in its scope
# prompt (`spec_draft`), and a turn spent re-fetching it is a turn not spent reading the doc that
# decides the question.
SCOPE_TOOLS = ("search_docs", "read_doc", "set_scope", "ask_user")
IMPACT_TOOLS = (
    "read_doc",
    "doc_behaviours",
    "search_docs",
    "doc_history",
    "search_history",
    "submit_impact",
    "ask_user",
)


@dataclass
class DocToolbox:
    """The tools for one stage of one draft, and the state they share.

    One instance per stage run, for the reason `tools.Toolbox` is one per run: the budget and the
    call log are per-run, and a toolbox reused across stages carries one stage's spend into the next.
    `read` is every doc actually opened, which the draft reports as its sources.
    """

    repo_root: Path
    names: tuple[str, ...] = SCOPE_TOOLS

    spent: int = 0
    read: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        every = {tool.name: tool for tool in self._build_tools()}
        self._tools = [every[name] for name in self.names]
        self._by_name = {tool.name: tool for tool in self._tools}

    @property
    def remaining(self) -> int:
        return max(0, DRAFT_TOOL_BUDGET - self.spent)

    def _charge(self, text: str) -> str:
        """Billed on the way out, as `tools.Toolbox._charge` is: the call that spends the last of
        the budget still returns in full, and only the next one is refused."""
        self.spent += len(text)
        return text

    def _exhausted(self) -> str:
        return (
            "Tool budget spent — nothing more can be read this stage. Finish from what you have "
            "already seen."
        )

    # --- the reading tools, as text ---------------------------------------------------------------

    def search_docs(self, query: str, domain: str = "", limit: int = SEARCH_LIMIT_DEFAULT) -> str:
        if not self.remaining:
            return self._exhausted()
        hits = search_docs(self.repo_root, str(query or ""), domain or None, limit)
        if not hits:
            return f"No docs match {query!r}{f' in {domain}' if domain else ''}."
        return self._charge(
            "\n".join(f"{h['path']} — {h['title']}: {h['snippet']}" for h in hits)
        )

    def read_doc(self, path: str) -> str:
        if not self.remaining:
            return self._exhausted()
        try:
            doc, rel = resolve_doc(self.repo_root, path)
        except ValueError as exc:
            return f"read_doc: {exc}."
        # Recorded the way a source is cited — repo-relative — whichever spelling the model used.
        if rel not in self.read:
            self.read.append(rel)
        return self._charge(source.clip(doc.read_text(), min(READ_DOC_CHARS, self.remaining)))

    def doc_behaviours(self, path: str) -> str:
        if not self.remaining:
            return self._exhausted()
        try:
            rows = doc_behaviours(self.repo_root, path)
        except ValueError as exc:
            return f"doc_behaviours: {exc}."
        if not rows:
            return (
                f"{path} states no behaviours in How It Works, Outcomes, Edge Cases or "
                "Acceptance Tests."
            )
        return self._charge(behaviours_text(rows))

    def doc_history(self, path: str) -> str:
        if not self.remaining:
            return self._exhausted()
        try:
            commits = doc_history(self.repo_root, path)
        except ValueError as exc:
            return f"doc_history: {exc}."
        if not commits:
            return f"No indexed commits are linked to {path}."
        return self._charge(
            "\n".join(f"{c['sha'][:8]} {c['date'][:10]} {c['message']}" for c in commits)
        )

    def search_history(self, query: str) -> str:
        if not self.remaining:
            return self._exhausted()
        commits = search_history(self.repo_root, str(query or ""))
        if not commits:
            return f"No commits match {query!r}."
        return self._charge(
            "\n\n".join(f"{c['sha']} {c['message']}\n{c['summary']}" for c in commits)
        )

    # --- the terminal tools -----------------------------------------------------------------------

    def set_scope(
        self,
        domain: str = "",
        topic: str = "",
        type: str = "feature",  # noqa: A002 — the model-facing name, as in tools.submit_doc
        existing_doc: str | None = None,
        reason: str = "",
    ) -> str:
        payload = scope_payload(self.repo_root, domain, topic, type, existing_doc, reason)
        if isinstance(payload, str):
            return payload  # not terminal: the model sees what was wrong and corrects it
        raise StageDone("scope", payload)

    def ask_user(self, question: str = "", options: Any = None) -> str:
        payload = question_payload(question, options)
        if isinstance(payload, str):
            return payload
        if "set_scope" not in self.names:
            # Past the scope stage an option is an answer, not a place. A real model attached
            # `existing_doc` to every option of an impact question ("what should the reader see
            # when the retry fails too?"), and kept, that would have moved the draft to whichever
            # doc the clicked answer happened to name.
            payload["options"] = [{"label": option["label"]} for option in payload["options"]]
        raise StageDone("ask", payload)

    def submit_impact(self, summary: str = "", changes: Any = None, behaviours: Any = None) -> str:
        payload = impact_payload(summary, changes, behaviours)
        if isinstance(payload, str):
            return payload
        raise StageDone("impact", payload)

    # --- the list handed to a provider ------------------------------------------------------------

    def tools(self) -> list[Tool]:
        return self._tools

    def invoke(self, name: str, arguments: dict) -> str:
        """Run one tool by name, logging it and containing its failures — except `StageDone`,
        which is the stage ending on purpose (`tools.Toolbox.invoke`, same contract)."""
        tool = self._by_name.get(name)
        shown = ", ".join(f"{k}={v!r}" for k, v in list(arguments.items())[:3])
        self.calls.append(f"{name}({clip(shown, 120)})")
        if tool is None:
            return f"No tool named {name!r} on this stage."
        try:
            return tool.run(**arguments)
        except StageDone:
            raise
        except TypeError as exc:
            return f"{name}: {exc}"

    def _build_tools(self) -> list[Tool]:
        docs = paths.docs_root(self.repo_root).name
        path_arg = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": f"e.g. '{docs}/billing/refund-flow.md'."}
            },
            "required": ["path"],
        }
        option = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "What the reader clicks."},
                "domain": {"type": "string", "description": "The domain this option places it in."},
                "topic": {"type": "string", "description": "kebab-case topic, for a new doc."},
                "type": {"type": "string", "enum": list(DOC_TYPES)},
                "existing_doc": {"type": "string", "description": "An existing doc it updates."},
            },
            "required": ["label"],
        }
        return [
            Tool(
                name="search_docs",
                description=(
                    "Full-text search over the docs, ranked. Use the words a reader would use. "
                    "Pass domain to search one folder only."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "domain": {"type": "string", "description": "Optional domain filter."},
                        "limit": {"type": "integer", "description": f"Up to {SEARCH_LIMIT_MAX}."},
                    },
                    "required": ["query"],
                },
                run=self.search_docs,
            ),
            Tool(
                name="read_doc",
                description="Read one doc in full, frontmatter included.",
                schema=path_arg,
                run=self.read_doc,
            ),
            Tool(
                name="doc_behaviours",
                description=(
                    "The behaviours one doc states, one per line with a stable id: STEP-n (How It "
                    "Works), OUT-n (Outcomes), EDGE-n (Edge Cases), AT-n (Acceptance Tests). Use "
                    "these ids as `ref` in submit_impact."
                ),
                schema=path_arg,
                run=self.doc_behaviours,
            ),
            Tool(
                name="doc_history",
                description="Commits linked to one doc, most recent first — how it got this way.",
                schema=path_arg,
                run=self.doc_history,
            ),
            Tool(
                name="search_history",
                description=(
                    "Search commit messages and their summaries — what used to be true, and why it "
                    "changed."
                ),
                schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                run=self.search_history,
            ),
            Tool(
                name="set_scope",
                description=(
                    "Decide where the change belongs. Ends this stage. existing_doc when a doc "
                    "already covers the subject (the change updates it); otherwise domain + topic "
                    "for a new doc."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "kebab-case domain, existing where one fits.",
                        },
                        "topic": {
                            "type": "string",
                            "description": "kebab-case topic slug. Never 'readme'.",
                        },
                        "type": {
                            "type": "string",
                            "enum": list(DOC_TYPES),
                            "description": "'workflow' for a multi-step process, 'feature' otherwise.",
                        },
                        "existing_doc": {
                            "type": "string",
                            "description": f"Path of the doc this updates, e.g. '{docs}/chat/foo.md'.",
                        },
                        "reason": {"type": "string", "description": "One sentence: why here."},
                    },
                    "required": ["domain", "topic", "type"],
                },
                run=self.set_scope,
            ),
            Tool(
                name="ask_user",
                description=(
                    "Ask the reader one short question instead of guessing. Ends this stage; their "
                    "answer comes back on the next run. Give 2-5 options they can click."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": option},
                    },
                    "required": ["question"],
                },
                run=self.ask_user,
            ),
            Tool(
                name="submit_impact",
                description=(
                    "Hand back what the change does to the docs. Ends this stage. List only what "
                    "changes — never behaviours that stay the same."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "1-2 sentences: what the reader gets.",
                        },
                        "changes": {
                            "type": "array",
                            "description": "Each section of the target doc that changes.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "section": {"type": "string"},
                                    "kind": {"type": "string", "enum": list(CHANGE_KINDS)},
                                    "summary": {"type": "string"},
                                },
                                "required": ["section", "kind", "summary"],
                            },
                        },
                        "behaviours": {
                            "type": "array",
                            "description": "Each behaviour that changes, appears or disappears.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ref": {
                                        "type": "string",
                                        "description": "Row id it changes (AT-3), '<doc path>#<id>' "
                                        "for another doc, or empty for a new behaviour.",
                                    },
                                    "behaviour": {"type": "string"},
                                    "today": {
                                        "type": "string",
                                        "description": "What happens now, per the docs.",
                                    },
                                    "after": {"type": "string", "description": "What will happen."},
                                    "kind": {"type": "string", "enum": list(BEHAVIOUR_KINDS)},
                                },
                                "required": ["behaviour", "today", "after", "kind"],
                            },
                        },
                    },
                    "required": ["summary", "changes", "behaviours"],
                },
                run=self.submit_impact,
            ),
        ]
