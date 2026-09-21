"""The tools a model is handed when it writes a doc, and the budgets that stop it browsing forever.

This is the half of `specky document` that changes what specky *is*. Everywhere else, the model
never browses: `commit_doc.py` pastes a diff into a prompt, and every byte it sees was chosen in
Python. That works when the subject is a change, because a diff is a complete statement of one. It
does not work when the subject is a feature, because a feature is spread across files nothing in
Python can reliably guess at — which is why the doc set it produced was broad and shallow.

So the model searches for itself. What it gets is deliberately small: find files, look at what's in
one without opening it, open one, read a doc that already exists, and hand back the finished doc.
Five verbs. There is no write tool, no shell, and no way to name a path outside what
`source.source_files` returns — the model produces text and specky decides what reaches disk
(`document.py`).

Three things are tracked across a run, and each one exists because its absence is a real failure:

- **A result budget.** A loop without one re-reads the same large file until the context or the bill
  runs out. Past `TOOL_RESULT_BUDGET` every tool says so and returns nothing more, which is a
  prompt the model can act on rather than an error it can't.
- **The read set.** Every file actually opened. `document.py` refuses to write a doc when this is
  empty and intersects it with whatever the model claims in `sources:` — a doc written from no
  source at all is fabricated end to end and reads exactly like a real one.
- **A call log.** What was called with what, so a twelve-turn run prints as progress instead of
  sitting silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from specky import source
from specky.paths import doc_in_tree, docs_root

# How many characters of tool output one run may consume in total. Characters rather than tokens for
# the reason every other budget in specky is (`chat_server.CONTEXT_CHARS_MAX`,
# `commit_doc.DIFF_TRUNCATE_CHARS`): specky knows no provider's tokenizer, and a fabricated token
# count would be worse than an honest char count. 80k is several large modules read in full — far
# more than a single feature needs, which is the point. It is a runaway guard, not a working limit.
TOOL_RESULT_BUDGET = 80_000

# How much of one file a single `read_file` returns. A model that asks for a 60k-line module gets
# its head and a truncation notice, and can ask for a later line range if it wants the rest.
READ_FILE_CHARS = 24_000

# Hits one search returns. Past this the query was too broad to be worth answering literally, and
# the right move is a narrower one rather than a longer list.
SEARCH_LIMIT_MAX = 100
SEARCH_LIMIT_DEFAULT = 60

# Paths one `list_files` will name before it stops naming them and describes the shape instead. A
# bare `list_files()` on a real repo is thousands of lines that answer no question anyone asked, and
# it is charged against the run's budget — then resent on every turn afterwards.
LIST_FILES_MAX = 120

# Commits `history` returns for one path. Enough to see why something is the way it is; short of
# the whole log of a file somebody has been editing for five years.
HISTORY_COMMITS = 12


@dataclass(frozen=True)
class Tool:
    """One callable the model can reach, in the neutral shape both provider loops translate from.

    `schema` is JSON Schema for the input object: Anthropic sends it as `input_schema`, the
    OpenAI-compatible path nests it under `function.parameters`. Keeping it provider-neutral here is
    what stops the tool list being written twice.
    """

    name: str
    description: str
    schema: dict
    run: Callable[..., str]


@dataclass(frozen=True)
class Submission:
    """The finished doc, as the model handed it over."""

    domain: str
    topic: str
    doc_type: str
    tags: list[str]
    purpose: str
    sources: list[str]
    markdown: str
    glossary_terms: list[dict]


class DocSubmitted(Exception):
    """`submit_doc` was called — the loop is over.

    An exception rather than a return value because `submit_doc` is reached through the same
    `tool.run(**input)` path as every other tool, several layers inside a provider's loop. Unwinding
    says "this run is finished" exactly once, where a sentinel string would have to be checked for
    by both provider implementations and could be mistaken for a tool result.
    """

    def __init__(self, submission: Submission) -> None:
        super().__init__("doc submitted")
        self.submission = submission


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _string_list(value: Any) -> list[str]:
    """A list of non-empty strings out of whatever the model sent.

    Models send a bare string where a list was asked for often enough that refusing it would cost a
    whole run over a comma.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@dataclass
class Toolbox:
    """The tools for one `specky document` run, and the state they share.

    One instance per run: the budget, the read set and the call log are all per-run, and a Toolbox
    reused across runs would carry one feature's spend into the next.
    """

    repo_root: Path
    scope: str | None = None

    spent: int = 0
    read: list[str] = field(default_factory=list)  # ordered: the model's own reading order
    calls: list[str] = field(default_factory=list)

    # Derived in __post_init__, so they stay out of the dataclass's signature.
    allowed: set[str] = field(init=False, default_factory=set)
    listable: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        in_scope = source.source_files(self.repo_root, self.scope)
        self.allowed = set(in_scope)
        # Tests are reachable even when they sit outside the scope, and they almost always do:
        # `--scope src/billing` says where the feature lives, while its tests live in `tests/`.
        # Excluding them costs the doc its best material — a test case is a statement of expected
        # behaviour written by someone who had to make it pass, which is exactly the Acceptance
        # Tests table the model is being asked to produce. Scope is about where the *subject* is,
        # not about what may be consulted to describe it.
        if self.scope:
            self.allowed |= {
                path for path in source.source_files(self.repo_root) if source.is_test(path)
            }
        # Already sorted — `source_files` returns a sorted list.
        self.listable = in_scope

        # Built once. `tools()` used to construct seven `Tool`s and every schema literal on each
        # call, and `invoke` called it per tool call purely to look a name up — dozens of rebuilds
        # on a sixteen-turn run, and the list handed to the provider was a different object graph
        # from the one `invoke` resolved against.
        self._tools = self._build_tools()
        self._by_name = {tool.name: tool for tool in self._tools}

    # --- budget ---------------------------------------------------------------------------------

    @property
    def remaining(self) -> int:
        return max(0, TOOL_RESULT_BUDGET - self.spent)

    def _charge(self, text: str) -> str:
        """Bill a result against the run's budget and hand it back unchanged.

        Charged on the way *out* rather than checked on the way in, so the tool that spends the last
        of the budget still returns what it found in full. Only the next call is refused, which is
        why a single result may overshoot `TOOL_RESULT_BUDGET` — clipping a result to the remaining
        budget would cut a file off mid-line for the sake of an accounting figure.

        Only `read_file` and `read_doc` clip, and against their own per-call caps rather than this
        one, because they are the two that can be handed something arbitrarily large.
        """
        self.spent += len(text)
        return text

    def _exhausted(self) -> str:
        return (
            "Tool budget spent — no more of this repo can be read on this run. Write the doc from "
            "what you have already seen, and say plainly in it where the source ran out."
        )

    # --- the tools ------------------------------------------------------------------------------

    def search_code(self, query: str, regex: bool = False, limit: int = SEARCH_LIMIT_DEFAULT) -> str:
        query = _text(query)
        if not query:
            return "search_code needs a non-empty query."
        if not self.remaining:
            return self._exhausted()
        try:
            limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
        except (TypeError, ValueError):
            limit = SEARCH_LIMIT_DEFAULT
        hits = source.grep(
            self.repo_root, query, allowed=self.allowed, regex=bool(regex), limit=limit
        )
        if not hits:
            return f"No tracked source matches {query!r}."
        more = (
            f"\n\n(stopped at {limit} hits — narrow the query to see the rest)"
            if len(hits) >= limit
            else ""
        )
        return self._charge("\n".join(hits) + more)

    def list_files(self, prefix: str = "") -> str:
        """Paths under a prefix — or, when there are too many, the directories holding them.

        A bare call on a real repo used to return every tracked file. That is thousands of lines
        answering no question, charged against the budget and then resent on every later turn; the
        useful reply to "what is in this repo" is its shape, which is what the model was already
        given before it asked.
        """
        prefix = _text(prefix).lstrip("./")
        matched = [path for path in self.listable if not prefix or path.startswith(prefix)]
        if not matched:
            where = f" under {prefix!r}" if prefix else ""
            return f"No tracked source files{where}."
        if len(matched) > LIST_FILES_MAX:
            by_dir: dict[str, int] = {}
            for path in matched:
                by_dir[str(Path(path).parent)] = by_dir.get(str(Path(path).parent), 0) + 1
            shape = "\n".join(f"{d}/  {n} files" for d, n in sorted(by_dir.items()))
            return self._charge(
                f"{len(matched)} files match {prefix or 'the whole repo'} — too many to list. "
                "Narrow the prefix, or use search_code to find the ones you want. They live in:\n"
                + shape
            )
        return self._charge("\n".join(matched))

    def outline(self, paths: Any) -> str:
        """Header line and top-level definitions for each path — triage before spending a read."""
        wanted = _string_list(paths)
        if not wanted:
            return "outline needs one or more paths."
        if not self.remaining:
            return self._exhausted()

        blocks = []
        for rel_path in wanted:
            rel_path = rel_path.lstrip("./")
            if rel_path not in self.allowed:
                blocks.append(f"{rel_path} — not a tracked source file in scope")
                continue
            text = source.read_source(self.repo_root / rel_path)
            if text is None:
                blocks.append(f"{rel_path} — unreadable (binary, generated, or too large)")
                continue
            blocks.append(source.file_summary(rel_path, text))
        return self._charge("\n".join(blocks))

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        """A file's text with line numbers, optionally one range of it.

        This is the only tool that records into the read set, because it is the only one that shows
        a model what code actually *does*. A path that turned up in a search but was never opened
        did not inform the doc, and must not end up in its `sources:`.
        """
        rel_path = _text(path).lstrip("./")
        if not rel_path:
            return "read_file needs a path."
        if rel_path not in self.allowed:
            return (
                f"{rel_path} is not a tracked source file in scope. Use list_files or search_code "
                "to find the real path."
            )
        if not self.remaining:
            return self._exhausted()

        text = source.read_source(self.repo_root / rel_path)
        if text is None:
            return f"{rel_path} cannot be read (binary, generated, or over {source.MAX_FILE_BYTES} bytes)."

        lines = text.splitlines()
        try:
            first = max(1, int(start))
        except (TypeError, ValueError):
            first = 1
        try:
            last = len(lines) if end is None else min(len(lines), int(end))
        except (TypeError, ValueError):
            last = len(lines)
        if first > len(lines):
            return f"{rel_path} has {len(lines)} lines; {first} is past the end."

        numbered = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
        body = source.clip(numbered, min(READ_FILE_CHARS, self.remaining))
        if rel_path not in self.read:
            self.read.append(rel_path)
        header = f"--- {rel_path} (lines {first}-{last} of {len(lines)})"
        return self._charge(f"{header}\n{body}")

    def read_doc(self, path: str) -> str:
        """An existing doc under the docs root, so an update can see what it is editing.

        Separate from `read_file` and deliberately not in the read set: a doc is not evidence about
        the code. Reading one tells the model what prose already exists to preserve — which is what
        `generator.lost_content` will hold it to.
        """
        rel_path = _text(path).lstrip("./")
        if not rel_path:
            return "read_doc needs a path."
        candidate = doc_in_tree(self.repo_root, rel_path)
        if candidate is None:
            root = docs_root(self.repo_root)
            return f"{rel_path} is not under {root.name}/ — read_doc only reads the docs tree."
        if not candidate.is_file():
            return f"No doc at {rel_path} yet."
        if not self.remaining:
            return self._exhausted()

        text = candidate.read_text()
        # Say so *here*, where it can still change what the model does. Without this the freeze is
        # only discovered at the write, after a whole run has been paid for — which is exactly what
        # happened the first time this was pointed at a real repo: eighteen tool calls spent on a
        # doc that was never going to be written.
        from specky import frontmatter

        if str(frontmatter.parse(text)[0].get("authored", "")).strip().lower() == "human":
            text = (
                "NOTE: this doc is marked `authored: human` and will NOT be overwritten — a "
                "submission naming its domain and topic is refused. Either document a different "
                "subject, or stop and say that this one is owned by a person.\n\n" + text
            )
        return self._charge(source.clip(text, self.remaining))

    def history(self, path: str) -> str:
        """Recent commit subjects for one file — why it is the way it is.

        Code says what a system does; the log is the only place that says what it used to do and
        what stopped working when it did. A threshold with an odd value, a guard that looks
        redundant, an ordering that seems arbitrary — the commit that introduced it is usually the
        whole explanation, and it is the difference between a doc that restates the code and one
        that tells a reader something they could not have read for themselves.
        """
        rel_path = _text(path).lstrip("./")
        if not rel_path:
            return "history needs a path."
        if rel_path not in self.allowed:
            return f"{rel_path} is not a tracked source file in scope."
        if not self.remaining:
            return self._exhausted()
        try:
            log = source._git(
                self.repo_root,
                "log",
                f"-{HISTORY_COMMITS}",
                "--format=%h %ad %s",
                "--date=short",
                "--",
                rel_path,
            )
        except Exception as exc:  # a path git has never seen, a shallow clone, a broken repo
            return f"could not read history for {rel_path}: {exc}"
        if not log.strip():
            return f"No commits touch {rel_path} yet."
        return self._charge(f"--- {rel_path}, most recent first\n{log.strip()}")

    def submit_doc(
        self,
        domain: str,
        topic: str,
        type: str = "feature",  # noqa: A002 — the model-facing name; `type` is what the frontmatter key is called
        tags: Any = None,
        purpose: str = "",
        sources: Any = None,
        markdown: str = "",
        glossary_terms: Any = None,
    ) -> str:
        """Hand back the finished doc. Terminal — raises `DocSubmitted`.

        Nothing is validated here beyond shape. Whether the doc may actually be written is
        `document.py`'s decision, and it is made against the read set and the doc already on disk,
        neither of which the model is in a position to judge.
        """
        terms = []
        for raw in glossary_terms if isinstance(glossary_terms, list) else []:
            if isinstance(raw, dict) and _text(raw.get("term")) and _text(raw.get("definition")):
                terms.append(
                    {"term": _text(raw.get("term")), "definition": _text(raw.get("definition"))}
                )
        raise DocSubmitted(
            Submission(
                domain=_text(domain),
                topic=_text(topic),
                doc_type=type if type in ("feature", "workflow") else "feature",
                tags=_string_list(tags),
                purpose=_text(purpose),
                sources=[p.lstrip("./") for p in _string_list(sources)],
                markdown=str(markdown or ""),
                glossary_terms=terms,
            )
        )

    # --- the list handed to a provider ----------------------------------------------------------

    def tools(self) -> list[Tool]:
        return self._tools

    def _build_tools(self) -> list[Tool]:
        docs = docs_root(self.repo_root).name
        return [
            Tool(
                name="search_code",
                description=(
                    "Search this repo's tracked source files for a string (or a regex with "
                    "regex=true). Case-insensitive. Returns 'path:line: text' hits. This is how you "
                    "find the code behind a feature you have only been given a name for — start "
                    "here, with the words a user would use, then follow the names you find."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Text or regex to search for."},
                        "regex": {
                            "type": "boolean",
                            "description": "Treat query as an extended regex. Default false.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"Max hits, up to {SEARCH_LIMIT_MAX}.",
                        },
                    },
                    "required": ["query"],
                },
                run=self.search_code,
            ),
            Tool(
                name="list_files",
                description=(
                    "List tracked source files under a path prefix. Use it to see what a directory "
                    "holds before deciding what to open. Pass a prefix: without one, or with too "
                    "broad a one, you get the directories rather than the files."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": "Path prefix, e.g. 'src/billing'. Omit for everything.",
                        }
                    },
                },
                run=self.list_files,
            ),
            Tool(
                name="outline",
                description=(
                    "For each path, the file's header comment and its top-level definitions, "
                    "without its body. Much cheaper than read_file — use it to decide which files "
                    "are worth opening."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Repo-relative source paths.",
                        }
                    },
                    "required": ["paths"],
                },
                run=self.outline,
            ),
            Tool(
                name="read_file",
                description=(
                    "Read a tracked source file, with line numbers. Pass start/end to read one "
                    "range of a long file. Test files are readable too, and worth reading: a test "
                    "case is a statement of expected behaviour, which is what the doc's Acceptance "
                    "Tests should be built from. A doc written without reading any code is refused."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative source path."},
                        "start": {"type": "integer", "description": "First line, 1-based."},
                        "end": {"type": "integer", "description": "Last line, inclusive."},
                    },
                    "required": ["path"],
                },
                run=self.read_file,
            ),
            Tool(
                name="read_doc",
                description=(
                    f"Read a doc that already exists under {docs}/. Do this before documenting "
                    "anything an existing doc might already cover: updating it in place is right, "
                    "and a second doc on one subject is a defect. A rewrite that drops or guts the "
                    "sections already there will be refused."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": f"e.g. '{docs}/billing/refund-flow.md' or 'billing/refund-flow.md'.",
                        }
                    },
                    "required": ["path"],
                },
                run=self.read_doc,
            ),
            Tool(
                name="history",
                description=(
                    "The recent commit subjects touching one file, most recent first. Use it when "
                    "the code raises a question it cannot answer: an oddly specific threshold, a "
                    "guard that looks redundant, an ordering that seems arbitrary. The commit that "
                    "introduced it is usually the explanation, and that explanation is the part a "
                    "reader could not have worked out for themselves."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repo-relative source path."}
                    },
                    "required": ["path"],
                },
                run=self.history,
            ),
            Tool(
                name="submit_doc",
                description=(
                    "Hand back the finished doc. Call this exactly once, when the markdown is "
                    "complete — it ends the session."
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "kebab-case module/area, e.g. 'billing'. Names what the "
                            "system does, never a layer ('utils', 'models', 'helpers').",
                        },
                        "topic": {
                            "type": "string",
                            "description": "kebab-case topic slug, e.g. 'refund-flow'. Never 'readme'.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["feature", "workflow"],
                            "description": "'workflow' for a multi-step process, 'feature' for a bounded capability.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "1-3 kebab-case business concepts. Reuse an existing tag if one fits.",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "One line for the MODULES.md index table.",
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The one to three files this doc is ABOUT — not every "
                            "file you opened while looking. This is read back as a claim that "
                            "these files are documented here, so a file named by mistake stops "
                            "anyone being warned when it changes. Only files you read may appear.",
                        },
                        "markdown": {
                            "type": "string",
                            "description": "The complete doc body, starting at its '# ' heading. No "
                            "frontmatter — it is rendered from the fields above.",
                        },
                        "glossary_terms": {
                            "type": "array",
                            "description": "Business/domain terms this doc introduces that other "
                            "docs will reuse. Omit unless genuinely shared vocabulary.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "term": {"type": "string"},
                                    "definition": {"type": "string"},
                                },
                                "required": ["term", "definition"],
                            },
                        },
                    },
                    "required": ["domain", "topic", "type", "purpose", "markdown"],
                },
                run=self.submit_doc,
            ),
        ]

    def invoke(self, name: str, arguments: dict) -> str:
        """Run one tool by name, logging the call and containing its failures.

        A tool that raises returns its error as the tool result rather than ending the run: the
        model sent a bad argument, it can see that it did, and the next turn usually fixes it. The
        one exception is `DocSubmitted`, which is the run ending on purpose.
        """
        tool = self._by_name.get(name)
        shown = ", ".join(f"{k}={v!r}" for k, v in list(arguments.items())[:3])
        self.calls.append(f"{name}({source.clip(shown, 120)})")
        if tool is None:
            return f"No tool named {name!r}."
        try:
            return tool.run(**arguments)
        except DocSubmitted:
            raise
        except TypeError as exc:  # wrong or missing arguments for this tool's schema
            return f"{name}: {exc}"
