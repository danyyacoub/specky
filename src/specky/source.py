"""Reading a repo's own source code — the primitives, with none of the policy.

Everything else in specky reads git: `commit_doc.py` walks `git log` and the only source code that
ever reaches a model is a unified diff. That is the right shape once docs exist, and the wrong shape
for documenting a feature nobody has written about yet — a diff says what *changed*, not what a
thing *is*. So `document.py` reads the code instead, and this module is what it reads through.

The split matters: nothing here knows what a doc is, what a domain is or what a model is. These are
file-system and git questions with file-system and git answers — which is why the same four
functions back both the tools handed to the model (`tools.py`) and the repo tree pasted into its
prompt, with no second implementation to keep in step.

Two properties everything here inherits from `source_files`, and both are load-bearing:

- **Tracked files only.** `git ls-files` cannot see anything gitignored or untracked, so
  `node_modules/`, a `.venv/` and specky's own `.specky/` are excluded by construction rather than
  by a denylist that needs a new entry per ecosystem.
- **It is also the allowlist.** `grep` and every path-taking tool resolve against this list, so a
  model cannot read its way out of the repo, into a vendored tree, or into a path the repo already
  told `specky check` to ignore.
"""

from __future__ import annotations

import ast
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from specky import paths
from specky.check import CheckConfig

# Extensions treated as source. An allowlist rather than a denylist: the question being asked is
# "what does this system do", and the answer lives in code, not in the fixtures, lockfiles, CSV
# samples and generated protobufs that a denylist would have to chase one ecosystem at a time.
SOURCE_EXTENSIONS = frozenset(
    """.py .pyi .js .jsx .mjs .cjs .ts .tsx .go .rs .rb .java .kt .kts .scala .swift .m .mm
    .c .h .cc .cpp .hpp .cs .php .ex .exs .erl .clj .hs .lua .pl .r .sh .bash .sql .vue .svelte"""
    .split()
)

# A file bigger than this is not read. It is a bundle, a generated client, a checked-in dataset or a
# fixture, and its symbol list would be both enormous and useless.
MAX_FILE_BYTES = 200_000

# Mean line length above which a file is treated as machine-written. A minified bundle is one line
# of 400kB; hand-written source in every language here averages well under 100.
MAX_MEAN_LINE = 200

# How many symbols one file's outline contributes. A 4,000-line module with 300 methods would
# otherwise bury the answer in its own method list.
MAX_SYMBOLS_PER_FILE = 25

# Tracked paths that are never what a feature is about. `git ls-files` excludes anything gitignored,
# which is most of the problem — but a Go `vendor/`, a committed `node_modules/`, a generated
# protobuf module and a checked-in minified bundle are all *tracked*, so gitignore never sees them
# and they would otherwise compete with real code for a model's attention.
IGNORED_PATHS = (
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

# Filenames that are the way into a module, listed before its other files when something has to
# choose an order.
_ENTRY_STEMS = ("index", "main", "__init__", "app", "server", "routes", "api", "cli", "mod", "lib")

# How a test file is recognised. Tests rank last: they are the lowest information-per-character
# content in a repo for the question "what does this system do", and a doc written from a test suite
# describes the fixtures rather than the feature. They are still reachable — a test name is often
# the clearest statement of an expected behaviour in the whole repo, which is exactly what an
# Acceptance Tests table is made of.
_TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "tests/", "test/", "spec/", "__tests__/")


def _git(repo_root: Path, *args: str) -> str:
    # `errors="replace"` for the same reason `commit_doc._commit_info` uses it: paths are bytes
    # specky didn't write, and one filename in a legacy encoding must not abort the walk.
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, errors="replace", check=True
    ).stdout


def _vendored(path: str) -> bool:
    """Is this one of IGNORED_PATHS's tracked-but-not-ours paths?

    Prefix for a pattern ending in `/`, `fnmatch` otherwise — the same hybrid `CheckConfig.ignores`
    uses, so the two ignore lists behave identically and a `vendor/` entry means the same thing in
    both.
    """
    return any(
        path.startswith(pattern) or f"/{pattern}" in f"/{path}"
        if pattern.endswith("/")
        else fnmatch(Path(path).name, pattern)
        for pattern in IGNORED_PATHS
    )


def read_source(path: Path) -> str | None:
    """A source file's text, or None if it shouldn't be read at all.

    Every guard here is a real failure mode rather than defensive padding:

    - **Not a regular file.** `git ls-files` lists symlinks and submodule gitlinks; `open()` on the
      latter raises and a symlink can point anywhere, including out of the repo.
    - **Too big**, or **machine-written** (one 400kB line): a bundle or a generated client, whose
      contents are enormous and say nothing about what the system does.
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

    This is also the allowlist every path-taking tool resolves against, which is what stops a model
    reading outside the repo or into a tree the repo has already disowned.
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


def clip(text: str, cap: int) -> str:
    """Truncate, saying so. A silent cut reads to a model as a file that simply ends there, and it
    will happily document the half it was shown as the whole thing."""
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + f"\n…[truncated — {cap} of {len(text)} characters shown]"


# --- outlines -------------------------------------------------------------------------------------

# Top-level definitions, per language family. Deliberately regexes and not parsers: a parser per
# language is a dependency per language, and this only has to produce a *name list* good enough for
# a model to decide whether a file is worth opening. A miss costs one absent name; a parser that
# can't be installed costs the whole feature. Python is the exception — `ast` is in the stdlib, so
# it is used for `.py` and gets the module docstring for free.
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
# the one-line "what is this file" a header comment leads with, and the paragraphs under it are
# detail a model can go and read for itself.
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
    """One `path — header\\n  name, name, name` block: what a file is, without reading it.

    This is the triage step a model makes before spending its read budget — the cheapest possible
    answer to "is this the file I want".
    """
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


def rank(rel_path: str) -> tuple[int, int, str]:
    """Order files: entry points first, tests last, then alphabetically.

    An `index.ts` or `__init__.py` names what the directory exports, which is worth more to a reader
    working out what a module *is* than whichever sibling happens to be largest. Tests go to the
    back for the reason in `_TEST_MARKERS`.
    """
    stem = Path(rel_path).stem.lower()
    return (1 if is_test(rel_path) else 0, 0 if stem in _ENTRY_STEMS else 1, rel_path)


# --- the repo's shape -----------------------------------------------------------------------------


def tree(repo_root: Path, scope: str | None = None) -> str:
    """`src/billing/  12 files, 88KB` per directory — where code lives, and roughly how much.

    Cheap, small and stable across runs, so it goes in the cacheable half of a prompt as the map a
    model starts from. It is a *shape*, not an index: it says where to look, and the search and read
    tools answer everything after that.
    """
    files = source_files(repo_root, scope)
    by_dir: dict[str, list[str]] = {}
    for path in files:
        by_dir.setdefault(str(Path(path).parent), []).append(path)

    lines = []
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
        lines.append(f"{directory}/  {len(by_dir[directory])} files, {size // 1024}KB")
    return "\n".join(lines)


def grep(
    repo_root: Path,
    pattern: str,
    *,
    allowed: set[str],
    regex: bool = False,
    limit: int = 60,
) -> list[str]:
    """`path:line: text` for every file in `allowed` matching `pattern`.

    `git grep` rather than a Python walk, for the reason it is always `git grep` here: it already
    respects the index, it is fast on a large repo, and it never opens a file git doesn't track.

    Results are then filtered against `allowed`, which is not redundant twice over. Git grep honours
    `.gitignore` but knows nothing about `IGNORED_PATHS`, the docs tree or `[check] ignore`, so
    without the filter a search would return hits inside a committed `vendor/` tree — or inside the
    docs tree itself, where one generated doc's invention would ground the next one's. And the
    caller's allowlist is not always expressible as a pathspec: a scoped run still reaches the tests
    for its subject, which live outside the scope by construction.

    So the search is always repo-wide and narrowed afterwards. `git grep` over a whole repository is
    milliseconds, and getting the set right matters more than the pathspec would save.
    """
    if not allowed:
        return []

    tail = ["-i", "-e", pattern, "--", "."]

    def run(flavour: str):
        # `check=False`: git grep exits 1 for "no matches", which is an answer and not an error.
        return subprocess.run(
            ["git", "grep", "--no-color", "-n", "-I", flavour, *tail],
            cwd=repo_root,
            capture_output=True,
            text=True,
            errors="replace",
        )

    if not regex:
        result = run("--fixed-strings")
    else:
        # PCRE first, POSIX ERE if this git was built without it. The difference is not academic:
        # `\s` and `\w` are the two most natural things to write in a search and neither means
        # anything in ERE, so a model's first regex would silently match nothing at all.
        result = run("-P")
        if result.returncode > 1 or "perl" in result.stderr.lower():
            result = run("-E")

    hits = []
    for line in result.stdout.splitlines():
        path = line.split(":", 1)[0]
        if path in allowed:
            # Long lines are the norm in exactly the files worth skipping; one hit should never be
            # able to spend a whole search's budget.
            hits.append(clip(line, 300))
            if len(hits) >= limit:
                break
    return hits
