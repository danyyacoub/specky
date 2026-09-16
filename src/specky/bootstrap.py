"""`specky bootstrap` — a repo's first docs, written from its code rather than from its commits.

Everything else in specky is commit-driven: `commit_doc.py` walks `git log` and the only source
code that ever reaches a model is a unified diff. That works once a doc set exists — a diff is a
precise statement of what changed about a feature already described somewhere — but it is the wrong
shape for a repo that has no docs at all. A doc then exists only if some commit in the scanned
window happened to touch that feature, so a module written in one commit years ago and stable since
gets nothing, and what does get written is assembled from diffs rather than from the code as it
stands. The only way to widen that today is `specky sync --since <first commit>`, which is O(commits)
— hundreds of billable calls on a real history, for worse docs than this produces.

So this module reads the code. It is O(domains), not O(commits): one call to work out what the
domains *are*, then one call per domain to write its doc.

Three properties worth stating, because each is a decision:

- **The model never browses.** `ai_provider.Provider` is one method, `generate`, and one of its
  three implementations shells out to an arbitrary CLI. There are no tools and no
  multi-turn, so every byte of code the model sees is gathered deterministically here and pasted
  into a prompt. That constraint is what makes the budgeting below the substance of this module.
- **It is idempotent, and resumable without state.** A domain that already has a doc is skipped, so
  re-running documents only what is new. Nothing is persisted between runs: discovery is one cheap
  call and its result is diffed against what is on disk, which matters because `.specky/` is
  gitignored and a state file kept there would vanish on a fresh clone.
- **Nothing is committed.** Like `specky sync`, and for the same reason: a run that writes a
  repo's entire doc tree is exactly the change a human should read in `git status` first.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from specky import frontmatter, paths
from specky.ai_provider import Provider, TruncatedResponse, supports_batch
from specky.check import CheckConfig
from specky.generator import (
    DOC_STYLE_INSTRUCTIONS,
    TAG_GUIDANCE,
    DocSync,
    ExistingDocs,
    leading_json_object,
    stage_pending,
    strip_code_fence,
    ungrounded_flags,
    update_modules_index,
)

# Extensions treated as source. An allowlist rather than a denylist: the question being asked is
# "what does this system do", and the answer lives in code, not in the fixtures, lockfiles, CSV
# samples and generated protobufs that a denylist would have to chase one ecosystem at a time.
SOURCE_EXTENSIONS = frozenset(
    """.py .pyi .js .jsx .mjs .cjs .ts .tsx .go .rs .rb .java .kt .kts .scala .swift .m .mm
    .c .h .cc .cpp .hpp .cs .php .ex .exs .erl .clj .hs .lua .pl .r .sh .bash .sql .vue .svelte"""
    .split()
)

# Files that say what a project *is* before any of its code is read. Each is clipped to
# MANIFEST_CLIP — a manifest's value is its name, description, entry points and dependency list,
# all of which are near the top; the lockfile-sized tail underneath is noise.
MANIFEST_NAMES = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "mix.exs",
)

# How many domains one run will document before stopping and reporting the rest. Not a correctness
# bound — it's the spend bound, and the reason the whole command can't surprise someone who ran it
# on a monorepo. Re-running documents the next batch, so the cap costs patience rather than
# coverage.
MAX_DOMAINS = 25

# Char budgets. Characters rather than tokens because that is the unit every other budget in specky
# is expressed in (`chat_server.CONTEXT_CHARS_MAX`, `commit_doc.DIFF_TRUNCATE_CHARS`) — specky knows
# no provider's tokenizer, and a fabricated token count would be worse than an honest char count.
SYMBOL_BUDGET = 60_000  # the symbol index, the only part of the map that scales with file count
DOMAIN_SOURCE_BUDGET = 24_000  # source shown when writing one domain's doc; cf. chat_server's
README_CLIP = 6_000
MANIFEST_CLIP = 2_000

# A file bigger than this is not read. It is a bundle, a generated client, a checked-in dataset or a
# fixture, and its symbol list would be both enormous and useless.
MAX_FILE_BYTES = 200_000

# Mean line length above which a file is treated as machine-written. A minified bundle is one line
# of 400kB; hand-written source in every language here averages well under 100.
MAX_MEAN_LINE = 200

# How many symbols one file contributes. A 4,000-line module with 300 methods would otherwise spend
# a whole directory's allocation on itself.
MAX_SYMBOLS_PER_FILE = 25

# Smallest useful symbol-index entry (a path and a couple of names). Once less than this is left in
# the budget, the walk stops rather than reading files it can only discard.
MIN_SYMBOL_BLOCK = 80

# Tracked paths that are never what a domain is about. `git ls-files` excludes anything gitignored,
# which is most of the problem — but a Go `vendor/`, a committed `node_modules/`, a generated
# protobuf module and a checked-in minified bundle are all *tracked*, so gitignore never sees them
# and they would otherwise compete with real code for the map's budget.
BOOTSTRAP_IGNORE = (
    "vendor/",
    "third_party/",
    "node_modules/",
    "generated/",
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.pb.go",
    "*.generated.*",
)

# Filenames that are the way into a module, listed before its other files when the budget is spent.
_ENTRY_STEMS = ("index", "main", "__init__", "app", "server", "routes", "api", "cli", "mod", "lib")

# How a test file is recognised. Tests rank last: they are the lowest information-per-character
# content in a repo for the question "what does this system do", and a doc written from a test suite
# describes the fixtures rather than the feature. They are still shown when there is budget left —
# a test name is often the clearest statement of an expected behaviour in the whole repo.
_TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "tests/", "test/", "spec/", "__tests__/")

# How many source paths a doc records in its `sources:` frontmatter. The list is a coverage hint for
# `specky check`, not an inventory: a domain resolving to 400 files would otherwise write a 400-line
# frontmatter block onto a doc a human has to read.
MAX_SOURCES_RECORDED = 40


@dataclass(frozen=True)
class Domain:
    """One `specs/<domain>/<topic>.md` the discovery call proposed."""

    domain: str
    topic: str
    purpose: str
    doc_type: str
    tags: list[str]
    paths: list[str]

    @property
    def name(self) -> str:
        """`"billing/refund-flow"` — the key `ExistingDocs.purposes` is keyed by."""
        return f"{self.domain}/{self.topic}"

    @property
    def rel_path(self) -> str:
        return f"{self.name}.md"


@dataclass(frozen=True)
class Discovery:
    product: dict
    domains: list[Domain]


@dataclass
class RepoMap:
    """What the model is told about a repo before it is asked anything.

    Four tiers, cheapest and most reliable first, because they degrade in that order: the tree is
    affordable on any repo, manifests and README are bounded by their own size, and only the symbol
    index scales with file count — so it is the one that gets budgeted.
    """

    tree: str = ""
    manifests: str = ""
    readme: str = ""
    symbols: str = ""
    files: list[str] = field(default_factory=list)
    skipped: int = 0

    def as_prompt(self) -> str:
        parts = [f"Repository layout (directory: source files, total size):\n{self.tree}"]
        # Every reduction is declared. A silently partial index reads to the model as a complete
        # one, and it will confidently describe the repo it was shown as the whole repo — the same
        # reason `_clip` announces its cut rather than just slicing.
        if self.manifests:
            parts.append(f"Package manifests:\n{self.manifests}")
        if self.readme:
            parts.append(f"README:\n{self.readme}")
        if self.symbols:
            note = (
                f" {self.skipped} of {len(self.files)} files are not listed here, for space — "
                "the layout above is the complete picture of where code lives."
                if self.skipped
                else ""
            )
            parts.append(
                "Source index (path — what the file's header says; then its top-level "
                f"definitions).{note}\n{self.symbols}"
            )
        return "\n\n".join(parts)


# --- reading the repo ---------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> str:
    # `errors="replace"` for the same reason `commit_doc._commit_info` uses it: paths are bytes
    # specky didn't write, and one filename in a legacy encoding must not abort the walk.
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, errors="replace", check=True
    ).stdout


def _vendored(path: str) -> bool:
    """Is this one of BOOTSTRAP_IGNORE's tracked-but-not-ours paths?

    Prefix for a pattern ending in `/`, `fnmatch` otherwise — the same hybrid `CheckConfig.ignores`
    uses, so the two ignore lists behave identically and a `vendor/` entry means the same thing in
    both.
    """
    return any(
        path.startswith(pattern) or f"/{pattern}" in f"/{path}"
        if pattern.endswith("/")
        else fnmatch(Path(path).name, pattern)
        for pattern in BOOTSTRAP_IGNORE
    )


def read_source(path: Path) -> str | None:
    """A source file's text, or None if it shouldn't be read at all.

    Every guard here is a real failure mode rather than defensive padding:

    - **Not a regular file.** `git ls-files` lists symlinks and submodule gitlinks; `open()` on the
      latter raises and a symlink can point anywhere, including out of the repo.
    - **Too big**, or **machine-written** (one 400kB line): a bundle or a generated client, whose
      symbol list is enormous and says nothing about what the system does.
    - **Binary.** A NUL in the first block is the same test git itself uses to decide a file isn't
      text.

    The decode is `errors="replace"`, which is the landmine `commit_doc._commit_info:167` already
    documents: a CP1252-saved source file has no early NUL, so it passes the binary test and then
    raises `UnicodeDecodeError` on a strict decode — aborting a whole run over one file. A U+FFFD in
    a prompt costs nothing.
    """
    try:
        if not path.is_file() or path.is_symlink():
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") + 1
    if len(text) / lines > MAX_MEAN_LINE:
        return None
    return text


def source_files(repo_root: Path, scope: str | None = None) -> list[str]:
    """Every tracked source path, oldest trick in `adopt.py` and the load-bearing choice here too.

    `git ls-files` rather than an `rglob`: it cannot see anything gitignored or untracked, so
    `node_modules/`, a `.venv/`, a vendored dependency and specky's own `.specky/` are excluded by
    construction rather than by a denylist needing a new entry per ecosystem
    (`adopt._tracked_markdown` makes the same argument at length).

    On top of that, two filters that aren't about ignorability: the docs tree itself, and whatever
    `[check] ignore` already lists — a repo that told `specky check` a path isn't code worth
    documenting has said the same thing this walk needs to know.
    """
    docs_prefix = paths.docs_prefix(repo_root)
    ignore = CheckConfig.load(repo_root)
    args = ["ls-files", "--"]
    args += [f"{scope.rstrip('/')}/*", scope] if scope else ["."]
    return sorted(
        path
        for path in _git(repo_root, *args).splitlines()
        if path
        and Path(path).suffix in SOURCE_EXTENSIONS
        and not path.startswith(docs_prefix)
        and not _vendored(path)
        and not ignore.ignores(path)
    )


# --- the symbol index ---------------------------------------------------------------------------

# Top-level definitions, per language family. Deliberately regexes and not parsers: a parser per
# language is a dependency per language, and this only has to produce a *name list* good enough for
# a model to recognise a module's shape. A miss costs one absent name; a parser that can't be
# installed costs the whole feature. Python is the exception — `ast` is in the stdlib, so it is used
# for `.py` and gets the module docstring for free.
_SYMBOL_PATTERNS = tuple(
    re.compile(pattern, re.MULTILINE)
    for pattern in (
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\*?([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?(?:declare\s+)?(?:type|interface|enum)\s+([A-Za-z_$][\w$]*)",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?[(<]",
        r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",  # go
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)",  # rust
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|trait|enum|impl)\s+([A-Za-z_]\w*)",
        r"^\s*(?:public|private|protected|internal)\s+(?:static\s+|final\s+|abstract\s+|sealed\s+)*"
        r"(?:class|interface|record|enum|struct)\s+([A-Za-z_]\w*)",  # java / c#
        r"^\s*(?:def|module)\s+([A-Za-z_][\w.]*[?!]?)",  # ruby / elixir
        r"^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION)\s+)([A-Za-z_][\w.]*)",  # sql
    )
)

# A file's opening comment, whatever the language spells it with. Only the first line is kept: it is
# the one-line "what is this file" a header comment leads with, and the paragraphs under it are the
# detail the per-domain pass will show in full anyway.
_HEADER_COMMENT = re.compile(r"^\s*(?://+|#+|/\*+|\*|--|\"\"\"|''')\s*(.+?)\s*(?:\*/|\"\"\"|''')?$")


def _header(text: str) -> str:
    for line in text.splitlines()[:15]:
        if not line.strip() or line.lstrip().startswith(("#!", "# -*-", "// @", "/* eslint")):
            continue
        match = _HEADER_COMMENT.match(line)
        if match and len(match.group(1)) > 8:
            return match.group(1)[:160]
        if line.strip():
            return ""  # first real line is code, so there's no header comment
    return ""


def _python_symbols(text: str) -> tuple[str, list[str]]:
    """`(module docstring's first line, top-level public names)` — `ast` rather than the regexes.

    Python gets the exception because `ast` is in the stdlib, so it costs no dependency and is
    exact where a regex guesses. Leading-underscore names are dropped: a module's private helpers
    are not what a reader is trying to recognise it by.
    """
    tree = ast.parse(text)
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    ]
    docstring = (ast.get_docstring(tree) or "").strip().splitlines()
    return (docstring[0][:160] if docstring else ""), names


def file_summary(rel_path: str, text: str) -> str:
    """One `path — header\\n  name, name, name` block for the symbol index."""
    header, names = "", []
    if rel_path.endswith((".py", ".pyi")):
        try:
            header, names = _python_symbols(text)
        except SyntaxError:  # a py2 file, a template, a deliberately broken fixture
            header = _header(text)
    if not names:
        seen: dict[str, None] = {}
        for pattern in _SYMBOL_PATTERNS:
            for name in pattern.findall(text):
                seen.setdefault(name, None)
        names = list(seen)
        header = header or _header(text)
    line = f"{rel_path}" + (f" — {header}" if header else "")
    if names:
        line += "\n  " + ", ".join(names[:MAX_SYMBOLS_PER_FILE])
    return line


def is_test(rel_path: str) -> bool:
    return any(marker in rel_path.lower() for marker in _TEST_MARKERS)


def _rank(rel_path: str) -> tuple[int, int, str]:
    """Order files: entry points first, tests last, then alphabetically.

    An `index.ts` or `__init__.py` names what the directory exports, which is worth more to a reader
    working out what a module *is* than whichever sibling happens to be largest. Tests go to the
    back for the reason in `_TEST_MARKERS`.
    """
    stem = Path(rel_path).stem.lower()
    return (1 if is_test(rel_path) else 0, 0 if stem in _ENTRY_STEMS else 1, rel_path)


def _symbol_index(repo_root: Path, files: list[str]) -> tuple[str, int]:
    """The budgeted symbol index, and how many files it had no room for.

    Filled round-robin across directories — every directory's best file, then every directory's
    second-best, and so on — rather than directory by directory. Two properties fall out of that,
    and both are the point:

    - **Breadth first.** You cannot discover a domain you were never shown, so the budget must not
      be spent in full on `src/legacy/` before `src/billing/` is reached. Round-robin guarantees
      every directory is represented before any directory gets seconds.
    - **Bigger directories still get more**, without weighting anything: a 300-file package has
      files left to offer in round 300, a 2-file one runs out in round 2.

    The cap is hard. It is checked against the block that is about to be appended, not the one
    already there — the earlier version checked after the fact and so could overrun by one block
    per directory, which on a 20-directory repo was 1.5kB over a 60kB budget.
    """
    by_dir: dict[str, list[str]] = {}
    for path in files:
        by_dir.setdefault(str(Path(path).parent), []).append(path)
    # Test directories go to the back of the round-robin, so under budget pressure they are what
    # loses its turn rather than a package of real code.
    queues = {
        d: sorted(paths_, key=_rank)
        for d, paths_ in sorted(by_dir.items(), key=lambda kv: (is_test(kv[0] + "/"), kv[0]))
    }

    blocks: list[str] = []
    spent = 0
    read = 0
    for depth in range(max(len(q) for q in queues.values())):
        for rel_paths in queues.values():
            if depth >= len(rel_paths):
                continue
            # Once the budget can't fit even a minimal entry, stop reading. Without this the walk
            # opens, decodes and symbol-scans every remaining file only to discard each one — on a
            # 10,000-file repo that is ~9,000 pointless file reads after the budget is gone.
            if spent + MIN_SYMBOL_BLOCK > SYMBOL_BUDGET:
                return "\n".join(blocks), len(files) - read
            rel_path = rel_paths[depth]
            text = read_source(repo_root / rel_path)
            read += 1
            if text is None:
                continue
            block = file_summary(rel_path, text)
            if spent + len(block) + 1 > SYMBOL_BUDGET:
                continue
            blocks.append(block)
            spent += len(block) + 1  # +1 for the newline the join adds
    return "\n".join(blocks), len(files) - len(blocks)


def repo_map(repo_root: Path, scope: str | None = None) -> RepoMap:
    """Everything the discovery call gets to see, assembled without calling a provider at all."""
    files = source_files(repo_root, scope)
    if not files:
        return RepoMap()

    by_dir: dict[str, list[str]] = {}
    for path in files:
        by_dir.setdefault(str(Path(path).parent), []).append(path)
    tree_lines = []
    for directory in sorted(by_dir):
        # `git ls-files` lists what the index holds, which on a sparse checkout or a partial clone
        # is not the same as what is on disk — and an `exists()` guard would still race a file
        # deleted between the two calls. The tree is a rough shape, so a missing file is a zero.
        size = 0
        for rel_path in by_dir[directory]:
            try:
                size += (repo_root / rel_path).stat().st_size
            except OSError:
                pass
        tree_lines.append(f"{directory}/  {len(by_dir[directory])} files, {size // 1024}KB")

    # Manifests near the root only. A monorepo has one per package — dozens of them — and the
    # question they are here to answer ("what is this project called, what does it depend on") is
    # answered by the top-level ones; the rest is repeated boilerplate that would crowd out the
    # symbol index underneath.
    manifests = []
    for rel_path in _git(repo_root, "ls-files", "--").splitlines():
        if Path(rel_path).name in MANIFEST_NAMES and rel_path.count("/") <= 1:
            text = read_source(repo_root / rel_path)
            if text:
                manifests.append(f"--- {rel_path}\n{_clip(text, MANIFEST_CLIP)}")

    readme = ""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        text = read_source(repo_root / name)
        if text:
            readme = _clip(text, README_CLIP)
            break

    symbols, skipped = _symbol_index(repo_root, files)
    return RepoMap(
        tree="\n".join(tree_lines),
        manifests="\n\n".join(manifests),
        readme=readme,
        symbols=symbols,
        files=files,
        skipped=skipped,
    )


def _clip(text: str, cap: int) -> str:
    """Truncate, saying so. Borrowed from `chat_server._truncate`: a silent cut reads to the model
    as a file that simply ends there, and it will happily document the half it was shown as the
    whole thing."""
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + f"\n…[truncated — {cap} of {len(text)} characters shown]"


# --- phase B: discovery -------------------------------------------------------------------------

# How many domains the discovery call is allowed to name. This is an *output* bound, not a spend
# bound (MAX_DOMAINS is the spend bound), and it exists because the whole answer has to fit inside
# `ai_provider.DEFAULT_MAX_TOKENS` — 4096. A response that hits that cap raises `TruncatedResponse`,
# which would abandon the run after paying for the single most expensive call it makes. Measured:
# 40 domains carrying per-file path lists lands almost exactly on the cap, which is why the prompt
# below asks for directory prefixes instead and leaves the glossary to its own call.
MAX_DISCOVERED = 40

DISCOVERY_PROMPT = """You are reading a codebase in order to plan its functional documentation. \
Below is a map of the repository: its layout, its package manifests, its README, and an index of \
its source files with their top-level definitions.

Answer with ONLY a JSON object, no other text:

{{"product": {{"what_it_is": "2-3 sentences, plain language, for a non-technical reader",
              "who_its_for": ["one line per audience"],
              "how_it_fits": ["one line per top-level piece and how the pieces relate"]}},
 "domains": [{{"domain": "kebab-case", "topic": "kebab-case", "purpose": "one line for an index \
table", "type": "feature|workflow", "tags": ["tag"], "paths": ["src/billing/"]}}]}}

- "domains": the feature/workflow areas this codebase actually has, each becoming one
  `{docs_root}/<domain>/<topic>.md`. Name them after what the system *does* for its users —
  "billing", "refund-flow", "rate-limits" — not after its layers: "utils", "helpers", "models",
  "controllers" and "components" are not domains. Group related files into one domain rather than
  emitting a doc per file; a repo of 500 files usually has between 5 and 30 real domains.
- "paths": where that domain lives, taken FROM THE MAP BELOW. Prefer a directory (with a trailing
  slash) over listing its files; at most 6 entries per domain, most central first. Never invent a
  path the map doesn't show.
- List at most {max_discovered} domains, most important first. Don't pad: an area nobody would ask
  a question about is not a domain.
{tag_guidance}

Docs that already exist (domain/topic — what it covers). If one of these already covers an area you
are about to name, answer with that doc's exact "domain" and "topic" rather than a near-duplicate —
a second doc on one subject is a defect:
{existing_docs}

{repo_map}
"""


def discover(repo_root: Path, provider: Provider, rmap: RepoMap, existing: ExistingDocs) -> Discovery:
    """Ask, once, what this repo is and what its domains are.

    One call rather than two, because the product framing and the domain list need the same
    whole-repo context and this is by far the largest prompt this module builds. The glossary is
    *not* asked for here: it would be the largest single chunk of this response's output budget
    (see MAX_DISCOVERED), and terms guessed from a file listing are worse than terms read back off
    the docs once they exist — so it gets its own call at the end, for one call out of ~27.

    Existing docs are listed for the same reason `CLASSIFY_PROMPT` lists them: on a re-run this is
    what stops `billing` coming back as `billing-and-invoicing` beside the doc that already exists.

    Parsed with `generator.leading_json_object`, which every JSON answer in specky goes through: a
    model that gets the object right and then adds a sentence after it is the common failure, and
    `json.loads` refuses trailing data.
    """
    prompt = DISCOVERY_PROMPT.format(
        docs_root=paths.docs_root(repo_root).name,
        max_discovered=MAX_DISCOVERED,
        tag_guidance=TAG_GUIDANCE.format(existing_tags=existing.tags_line()),
        existing_docs=existing.docs_block(),
        repo_map=rmap.as_prompt(),
    )
    try:
        raw = provider.generate(prompt, task="discovery")
    except TruncatedResponse as exc:
        # The one call here whose truncation is fatal — every later phase is per-domain and
        # contained. Re-raised with the lever, because "raise max_tokens" is not guessable from the
        # provider's own message when the thing that overflowed is a doc-planning answer.
        raise RuntimeError(
            f"discovery response hit the output token limit ({exc}). Raise `max_tokens` in "
            "specky.toml's [ai] table, or narrow the run with a path scope"
        ) from exc
    data = leading_json_object(strip_code_fence(raw))
    if data is None:
        raise RuntimeError("could not parse the discovery response as JSON")

    domains = []
    for raw in (data.get("domains") or [])[:MAX_DISCOVERED]:
        if not isinstance(raw, dict):
            continue
        domain, topic = _slug(raw.get("domain", "")), _slug(raw.get("topic", ""))
        if not domain or not topic:
            continue
        tags = [_slug(t) for t in raw.get("tags") or [] if isinstance(t, str)]
        domains.append(
            Domain(
                domain=domain,
                topic=topic,
                purpose=str(raw.get("purpose", "")).strip(),
                doc_type=raw.get("type") if raw.get("type") in ("feature", "workflow") else "feature",
                tags=[t for t in tags if t],
                paths=resolve_paths(raw.get("paths") or [], rmap.files),
            )
        )

    product = data.get("product") if isinstance(data.get("product"), dict) else {}
    return Discovery(product=product, domains=_dedupe(domains))


def resolve_paths(claimed: list, files: list[str]) -> list[str]:
    """The real source files behind whatever the model named, in the order it named them.

    Two jobs. It expands a directory (`"src/billing/"`) into the files under it, which is the form
    the prompt asks for because a per-file list across 40 domains is what pushes the response into
    the output token cap. And it drops anything that doesn't resolve — a model that invents
    `src/payments/` for a repo that spells it `lib/billing/` would otherwise have its domain
    documented from no source whatsoever, which is the one failure here that produces a confident,
    entirely fabricated doc. `write_domain_doc` skips a domain that resolves to nothing at all.
    """
    resolved: dict[str, None] = {}
    for entry in claimed:
        if not isinstance(entry, str) or not entry.strip():
            continue
        entry = entry.strip().lstrip("./")
        if entry in files:
            resolved.setdefault(entry, None)
            continue
        prefix = entry if entry.endswith("/") else f"{entry}/"
        for path in files:
            if path.startswith(prefix):
                resolved.setdefault(path, None)
    return list(resolved)


def _slug(text: str) -> str:
    """kebab-case, and never a path. A domain becomes a directory name and a topic becomes a
    filename, so a `/` or a `..` the model emitted has to be flattened before either is joined to
    the docs root."""
    from specky.adopt import kebab

    return kebab(str(text).replace("/", "-"))


def _dedupe(domains: list[Domain]) -> list[Domain]:
    """One doc per `domain/topic`. The discovery prompt asks for distinct domains, but a model that
    proposes `billing/refunds` twice would otherwise have the second one overwrite the first after
    paying for both."""
    seen: dict[str, Domain] = {}
    for domain in domains:
        seen.setdefault(domain.name, domain)
    return list(seen.values())


# --- phase C: one doc per domain ----------------------------------------------------------------

# Everything identical across every domain in a run, so it can be sent as a cacheable prefix and,
# under `--batch`, as the shared system block of every request in the batch. The per-domain half
# below carries what actually differs.
DOC_PREFIX = """You are writing reference documentation for one codebase, one feature or workflow \
per document. Write each from the source you are shown, staying grounded in what the code actually \
does — do not invent behaviour it doesn't show. Where source has been truncated, describe what you \
can see rather than guessing at the rest.

{product}"""

DOC_PROMPT = """Write specs/{domain}/{topic}.md. No doc exists for it yet.

What this feature/workflow is, in one line: {purpose}

--- Source for this feature/workflow ---
{source}
---

{style}
Output ONLY the final markdown content of the doc file, nothing else (no commentary, no code fences \
around it).
"""


def _domain_source(repo_root: Path, domain: Domain) -> str:
    """The source shown when writing one domain's doc, inside DOMAIN_SOURCE_BUDGET.

    Files are taken in the order discovery listed them, which it was asked to give most-central
    first — so when the budget runs out it is the periphery of a domain that gets clipped, not its
    entry point. A file that doesn't fit whole is included as a head rather than dropped: the top of
    a source file is its imports, its types and its principal entry points, which is most of what a
    functional description is drawn from.
    """
    blocks: list[str] = []
    spent = 0
    for rel_path in domain.paths:
        if spent >= DOMAIN_SOURCE_BUDGET:
            blocks.append(f"--- {rel_path}\n…[not shown — budget spent]")
            continue
        text = read_source(repo_root / rel_path)
        if text is None:
            continue
        clipped = _clip(text, DOMAIN_SOURCE_BUDGET - spent)
        blocks.append(f"--- {rel_path}\n{clipped}")
        spent += len(clipped)
    return "\n\n".join(blocks)


def _product_context(product: dict) -> str:
    """One line of product framing, carried into every per-domain call.

    The doc is written for a non-technical reader, and the same code reads very differently
    depending on what the product around it is for. It is also the only whole-repo context a
    per-domain call gets — the map itself is too big to repeat per domain.
    """
    what = str(product.get("what_it_is", "")).strip()
    return f"What this product is, for context:\n{what}\n" if what else ""


def domain_prompt(repo_root: Path, domain: Domain, product: dict) -> tuple[str, str] | None:
    """`(cacheable prefix, this domain's half)`, or None when there's nothing real to write from.

    The None case is checked before any call is made, so a domain whose paths didn't resolve costs
    nothing. It is the guard against the worst failure available here: a doc written from no source
    at all is fabricated end to end and reads exactly like a real one.

    Split out from `write_domain_doc` so the `--batch` path can build every domain's prompt up
    front and send them together; the serial path builds one and calls straight through.
    """
    source = _domain_source(repo_root, domain)
    if not source:
        return None
    return (
        DOC_PREFIX.format(product=_product_context(product)),
        DOC_PROMPT.format(
            domain=domain.domain,
            topic=domain.topic,
            purpose=domain.purpose or domain.name,
            source=source,
            style=DOC_STYLE_INSTRUCTIONS.format(
                domain_title=domain.domain.replace("-", " ").title(),
                topic_title=domain.topic.replace("-", " ").title(),
            ),
        ),
    )


def write_domain_doc(
    repo_root: Path,
    domain: Domain,
    provider: Provider,
    product: dict,
    existing: ExistingDocs,
    answer: str | None = None,
) -> DocSync:
    """Generate and write one `specs/<domain>/<topic>.md`.

    Returns a `DocSync` — the same type `generator.sync_feature_doc` returns, so `sync()` can report
    a bootstrap write and a commit-driven write through one code path. The guards are the same
    ones too: a doc naming a `--flag` nothing in this repo accepts is refused and parked in
    `.specky/pending/`, exactly as it would be on the commit path. `lost_content` has nothing to
    compare against here, because by construction there is no existing doc.

    `answer` is the generated body when the caller already has it — the `--batch` path fetches every
    domain's answer in one request and then walks them through here, so that batched and serial runs
    write, guard and report identically.
    """
    doc_path = paths.docs_root(repo_root) / domain.domain / f"{domain.topic}.md"
    rel = doc_path.relative_to(repo_root)

    if answer is None:
        built = domain_prompt(repo_root, domain, product)
        if built is None:
            return DocSync(doc_path, False, f"skipped {rel} — discovery named no source that exists")
        prefix, prompt = built
        answer = provider.generate(prompt, prefix=prefix, task="doc")

    # Strip any frontmatter the model produced: the real block is rendered below from discovery's
    # own classification, and a second one would land on top of it.
    _, body = frontmatter.parse(strip_code_fence(answer))
    body = body.strip() + "\n"

    # `sources:` records which files this doc was written from. It is what gives a bootstrapped repo
    # any `specky check` coverage at all: `indexer.index_doc_files` otherwise derives the code→doc
    # map from git log alone, and its DOC_FILES_MAX_DOCS_PER_COMMIT = 3 drops a commit that adds a
    # whole doc tree — so without this, every source file in a freshly bootstrapped repo reads as
    # undocumented. `generator.sync_feature_doc` preserves the key across later regenerations.
    meta: dict[str, str | list[str]] = {"type": domain.doc_type, "tags": domain.tags}
    if domain.paths:
        meta["sources"] = domain.paths[:MAX_SOURCES_RECORDED]

    invented = ungrounded_flags(repo_root, body)
    if invented:
        named = ", ".join(f"`{flag}`" for flag in invented)
        pending = stage_pending(
            repo_root, domain.domain, domain.topic, frontmatter.render(meta, body)
        )
        return DocSync(
            doc_path,
            False,
            f"refused {rel} — it names {named}, which nothing in this repo accepts. "
            f"Draft kept at {pending.relative_to(repo_root)}",
        )

    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(frontmatter.render(meta, body))
    update_modules_index(repo_root, domain.domain, domain.rel_path, domain.purpose)
    existing.record(domain.name, domain.purpose, domain.tags)
    return DocSync(doc_path, True, f"wrote {rel}")


# --- the two root meta docs -----------------------------------------------------------------------


def write_product(repo_root: Path, product: dict) -> Path | None:
    """`specs/PRODUCT.md`, or None if it already exists.

    Never overwritten. PRODUCT.md is the framing every other doc is written against — the
    `document-domain` skill reads it as an input at its step 3 — and it is the doc a team is most
    likely to have written deliberately. Generating over the top of that would be the one
    destructive thing this command could do.
    """
    path = paths.product_doc(repo_root)
    if path.exists():
        return None
    what = str(product.get("what_it_is", "")).strip()
    if not what:
        return None

    lines = [f"# {repo_root.name} — Product", "", "## What it is", "", what, ""]
    audiences = [str(a).strip() for a in product.get("who_its_for") or [] if str(a).strip()]
    if audiences:
        lines += ["## Who it's for", "", *[f"- {a}" for a in audiences], ""]
    pieces = [str(p).strip() for p in product.get("how_it_fits") or [] if str(p).strip()]
    if pieces:
        lines += ["## How it fits together", "", *[f"- {p}" for p in pieces], ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip("\n") + "\n")
    return path


GLOSSARY_PROMPT = """You maintain {docs_root}/GLOSSARY.md for this codebase: the shared vocabulary \
a new reader needs in order to read the docs below.

Respond with ONLY a JSON object, no other text:
{{"terms": [{{"term": "Refund window", "definition": "one or two sentences"}}]}}

- Business and domain concepts only — the words this system's own docs keep reusing. Not general
  programming terms ("regex", "cache", "API"), not generic product words ("user", "feature").
- At most 25 terms. A term must actually appear in the docs below; don't invent vocabulary.
- Neither field may contain a "|" character or a line break.
- These are already defined — don't repeat or redefine them: {existing_terms}

The docs this repo has, by title and what each one does:
{doc_summaries}
"""

# How much of the written docs the glossary call is shown. Each doc contributes its H1 and its
# `## What It Does` section, which is the part written in the vocabulary a glossary is made of.
GLOSSARY_CONTEXT_BUDGET = 16_000


def _doc_summaries(repo_root: Path, written: list[Path]) -> str:
    """Each written doc's title and `## What It Does`, within GLOSSARY_CONTEXT_BUDGET."""
    from specky.testgen import split_sections

    blocks: list[str] = []
    spent = 0
    for path in written:
        _, body = frontmatter.parse(path.read_text())
        title = next((l.lstrip("#").strip() for l in body.splitlines() if l.startswith("# ")), path.stem)
        what = next(
            ("\n".join(lines[1:]).strip() for title_, lines in split_sections(body)
             if title_.strip().lower() == "what it does"),
            "",
        )
        block = f"- {title}: {what}".strip()
        if spent + len(block) > GLOSSARY_CONTEXT_BUDGET:
            break
        blocks.append(block)
        spent += len(block)
    return "\n".join(blocks)


def sync_glossary(repo_root: Path, provider: Provider, written: list[Path]) -> tuple[Path | None, int]:
    """Append to `specs/GLOSSARY.md` the terms the docs just written are written in.

    Asked *after* the docs exist rather than alongside discovery, for two reasons: terms read back
    off real docs beat terms guessed from a file listing, and it keeps the largest single chunk out
    of the discovery response's output budget (see MAX_DISCOVERED).

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

    if not written:
        return None, 0
    path = paths.glossary(repo_root)
    existing = load_glossary(repo_root)

    prompt = GLOSSARY_PROMPT.format(
        docs_root=paths.docs_root(repo_root).name,
        existing_terms=", ".join(sorted(existing)) or "(none yet)",
        doc_summaries=_doc_summaries(repo_root, written),
    )
    data = leading_json_object(strip_code_fence(provider.generate(prompt, task="glossary"))) or {}

    rows = []
    for raw in (data.get("terms") or [])[:25]:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term", "")).strip()
        definition = " ".join(str(raw.get("definition", "")).split())
        if not term or not definition or "|" in term or "*" in term:
            continue
        if term.lower() in {t.lower() for t in existing}:
            continue  # hand-written definitions win
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


# --- orchestration --------------------------------------------------------------------------------


def needs_bootstrap(repo_root: Path) -> bool:
    """Would this repo get anything out of a bootstrap? Two conditions, and both matter.

    **No feature/workflow docs yet.** Asked of `ExistingDocs`, which already walks the docs root and
    already skips the `root` and `history` domains — so this is exactly "no
    `<docs root>/<domain>/<topic>.md` exists", and needs no state of its own. That matters:
    `.specky/` is gitignored, so a marker kept there would claim "never bootstrapped" on every
    fresh clone.

    **And some source to read.** Without this, every repo specky has never documented looks cold —
    including one that is all prose, all config, or empty — so `specky sync` would announce a
    bootstrap and then immediately report it had nothing to read. One `git ls-files` to say nothing
    instead.
    """
    return not ExistingDocs.load(repo_root).purposes and bool(source_files(repo_root))


def call_estimate(domains: int) -> str:
    """Discovery + one per domain + the glossary. Exact, unlike `commit_doc._call_estimate`'s
    range — nothing here is conditional on what the model decides."""
    return f"{2 + domains} AI calls"


def _confirm(pending: list[Domain], total: int, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{len(pending)} domain(s) to document ({call_estimate(len(pending))}) and stdin isn't "
            "a terminal — re-run with --yes, or narrow it with a path scope"
        )
    listed = ", ".join(d.name for d in pending[:5])
    more = f" and {len(pending) - 5} more" if len(pending) > 5 else ""
    print(f"specky bootstrap: {total} domain(s) found, {len(pending)} to document: {listed}{more}")
    if input(f"specky bootstrap: {call_estimate(len(pending))}. Continue? [y/N] ").strip().lower() not in (
        "y",
        "yes",
    ):
        raise RuntimeError("cancelled")


def _batch_domain_docs(
    repo_root: Path,
    domains: list[Domain],
    provider: Provider,
    product: dict,
    label: str,
) -> dict[str, str]:
    """Every domain's doc in one batched request, keyed by `domain/topic`.

    Returns `{}` on anything that goes wrong — a provider with no batch API, a batch that times
    out, a batch that errors. The caller then generates serially, which costs twice as much and
    works, rather than failing a run over an optimization.
    """
    if not supports_batch(provider, "doc"):
        print(f"{label}: --batch ignored, this provider has no batch API")
        return {}

    prompts = {}
    for domain in domains:
        built = domain_prompt(repo_root, domain, product)
        if built is not None:
            prompts[domain.name] = built
    if not prompts:
        return {}

    print(f"{label}: sending {len(prompts)} doc requests as one batch (this can take a while)")
    try:
        answers = provider.generate_batch(prompts, task="doc")
    except Exception as exc:
        print(f"{label}: batch failed ({exc}) — falling back to one call per domain")
        return {}
    missing = len(prompts) - len(answers)
    if missing:
        print(f"{label}: {missing} of {len(prompts)} came back empty — those are generated singly")
    return answers


def bootstrap(
    repo_root: Path,
    provider: Provider,
    max_domains: int = MAX_DOMAINS,
    scope: str | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    batch: bool = False,
    label: str = "specky bootstrap",
) -> list[Path]:
    """Read the code, work out the domains, write the docs. Returns what was written.

    Idempotent by construction and resumable without persisting anything: discovery runs every time
    (one call), and a domain that already has a doc on disk is dropped from the result rather than
    regenerated. So a second run documents only what is new, `--max-domains` caps how much of the
    remainder one run takes on, and re-running walks the rest — which terminates, because every run
    either writes a doc or reports that there is nothing left.

    A domain whose generation fails is reported and skipped, never fatal — same containment as
    `commit_doc._document`, and for the same reason: one bad file shouldn't abandon a run that has
    already paid for the rest.

    `batch` sends every domain's doc request in one Message Batches call, at half the per-token
    price. Each domain's doc depends on nothing but its own source, so there is no ordering to
    preserve — unlike the commit path, where classification has to stay serial. It is opt-in
    because a batch is asynchronous: the right trade for a one-off run of a repo's whole doc tree,
    the wrong one for anything anybody is waiting on.
    """
    rmap = repo_map(repo_root, scope)
    if not rmap.files:
        print(f"{label}: no source files found{f' under {scope}' if scope else ''}")
        return []
    print(
        f"{label}: {len(rmap.files)} source files"
        + (f", {rmap.skipped} not shown (budget or unreadable)" if rmap.skipped else "")
    )

    existing = ExistingDocs.load(repo_root)
    discovery = discover(repo_root, provider, rmap, existing)
    if not discovery.domains:
        print(f"{label}: no domains identified")
        return []

    # Already-documented domains drop out here rather than being asked about again — that, plus
    # re-running discovery each time, is the whole resume mechanism.
    pending = [d for d in discovery.domains if d.name not in existing.purposes]
    capped, deferred = pending[:max_domains], pending[max_domains:]

    if dry_run:
        print(f"{label}: {len(capped)} domain(s) to document, {call_estimate(len(capped))}")
        for i, domain in enumerate(capped, 1):
            print(f"  [{i}/{len(capped)}] {domain.name} — {domain.purpose}")
        for domain in deferred:
            print(f"  (deferred) {domain.name}")
        return []

    if not capped:
        print(f"{label}: every domain already has a doc")
        return []
    _confirm(capped, len(discovery.domains), assume_yes)

    written: list[Path] = []
    product_path = write_product(repo_root, discovery.product)
    if product_path:
        written.append(product_path)
        print(f"{label}: wrote {product_path.relative_to(repo_root)}")

    answers = _batch_domain_docs(repo_root, capped, provider, discovery.product, label) if batch else {}

    docs: list[Path] = []
    for i, domain in enumerate(capped, 1):
        try:
            result = write_domain_doc(
                repo_root, domain, provider, discovery.product, existing, answers.get(domain.name)
            )
            print(f"{label}: [{i}/{len(capped)}] {result.note}")
            if result.written:
                docs.append(result.path)
        except Exception as exc:
            print(f"{label}: [{i}/{len(capped)}] {domain.name} skipped ({exc})")
    written += docs

    glossary_path, added = sync_glossary(repo_root, provider, docs)
    if glossary_path:
        written.append(glossary_path)
        print(f"{label}: {added} term(s) into {glossary_path.relative_to(repo_root)}")

    if deferred:
        print(
            f"{label}: {len(deferred)} more domain(s) not documented (--max-domains {max_domains}) "
            "— run `specky bootstrap` again to continue"
        )
    return written
