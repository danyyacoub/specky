"""Where this repo's docs live — the one place `specs/` is spelled out.

The tree specky maintains is `<docs root>/<domain>/<topic>.md`, and `specs` is only the default
name for that root. It has to be configurable because plenty of repos already use `specs/` for
something else — OpenAPI documents, a Rust `specs` crate, an ECS module — and specky writing into
that directory would be a collision the repo can't undo. So every module asks here instead of
hardcoding the name, which is also what keeps `specky index`, `specky check` and the renderer from
ever disagreeing about which tree is being read.

Nothing in this module imports from the rest of specky: everything imports *it*, so it sits at the
bottom of the dependency graph and can't take part in a cycle.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_DOCS_ROOT = "specs"

# Where the per-commit changelog trail sits inside the docs root. Not configurable: it's an
# implementation detail of the history log, not a layout choice a repo has an opinion about.
HISTORY_DIR_NAME = "history"


def read_table(path: Path, keys: tuple[str, ...]) -> dict:
    """A nested TOML table, or `{}` if the file or any key along the way is absent."""
    if not path.exists():
        return {}
    with path.open("rb") as f:
        table = tomllib.load(f)
    for key in keys:
        table = table.get(key) or {}
    return table


@dataclass(frozen=True)
class DocsConfig:
    root: str = DEFAULT_DOCS_ROOT

    @classmethod
    def load(cls, repo_root: Path) -> DocsConfig:
        """`[docs]` in specky.toml, or `[tool.specky.docs]` in pyproject.toml.

        Same two spellings, same precedence and the same reason as `check.CheckConfig.load`:
        `specky init` writes a *gitignored* specky.toml, so a root kept only there is absent in CI
        — where `specky check` and `specky index` have to resolve the identical tree or the gate
        reads an empty one. pyproject.toml is committed, so that's the shared answer; specky.toml
        wins when it has a `[docs]` table, so a developer can still point somewhere else locally.
        """
        table = read_table(repo_root / "specky.toml", ("docs",)) or read_table(
            repo_root / "pyproject.toml", ("tool", "specky", "docs")
        )
        raw = str(table.get("root") or DEFAULT_DOCS_ROOT).strip()
        root = raw.strip("/")
        # An absolute or escaping root would put docs outside the repo, which every consumer here
        # assumes is impossible: paths are stored in the index relative to the repo root and
        # handed to git as pathspecs. `raw` is what's tested for absoluteness, not `root` — the
        # slash-strip that tidies `"documentation/"` would otherwise turn `/etc/specs` into the
        # perfectly valid-looking relative path `etc/specs`.
        if not root or raw.startswith("/") or ".." in Path(root).parts:
            root = DEFAULT_DOCS_ROOT
        return cls(root=root)


@lru_cache(maxsize=None)
def _config(repo_root: Path) -> DocsConfig:
    """Cached per repo root: this is read on the hot path (the post-commit hook resolves it several
    times per fire) and a TOML parse per call would be wasted work. Tests that rewrite the config
    mid-process call `reset_cache()`."""
    return DocsConfig.load(repo_root)


def reset_cache() -> None:
    _config.cache_clear()


def docs_root(repo_root: Path) -> Path:
    """`<repo>/specs` — the tree of `<domain>/<topic>.md` docs specky maintains."""
    return repo_root / _config(repo_root).root


def docs_prefix(repo_root: Path) -> str:
    """`"specs/"` — for `str.startswith` on index paths and as a git pathspec."""
    return f"{_config(repo_root).root}/"


def doc_in_tree(repo_root: Path, rel_path: str) -> Path | None:
    """Where a doc path a model or a client named points, or None if it leaves the docs tree.

    Accepts both spellings a caller reaches for — `specs/billing/refund.md` and `billing/refund.md`.
    The containment check runs on the resolved path, so `..` and a symlink out of the tree are
    caught alike. The path may not exist yet; whether that matters is the caller's business.
    """
    root = docs_root(repo_root)
    candidate = repo_root / rel_path
    if not candidate.is_file():
        candidate = root / rel_path
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def history_dir(repo_root: Path) -> Path:
    """`<repo>/specs/history` — one markdown file per commit."""
    return docs_root(repo_root) / HISTORY_DIR_NAME


def history_prefix(repo_root: Path) -> str:
    """`"specs/history/"` — for `str.startswith` on index paths."""
    return f"{_config(repo_root).root}/{HISTORY_DIR_NAME}/"


def modules_index(repo_root: Path) -> Path:
    return docs_root(repo_root) / "MODULES.md"


def product_doc(repo_root: Path) -> Path:
    """`<repo>/specs/PRODUCT.md` — what the product is, the framing every other doc is written
    against."""
    return docs_root(repo_root) / "PRODUCT.md"


def glossary(repo_root: Path) -> Path:
    """`<repo>/specs/GLOSSARY.md` — the shared vocabulary, parsed back by the viewer for term
    auto-linking (`html_render.load_glossary`)."""
    return docs_root(repo_root) / "GLOSSARY.md"


STATE_DIR_NAME = ".specky"


def state_dir(repo_root: Path) -> Path:
    """`<repo>/.specky`, created if missing — the index, the rendered site, locks and ledgers.

    Everything in it is derived or per-checkout, so it must never reach a commit. It ignores itself
    (a `.gitignore` of `*`, as `.pytest_cache` and `.ruff_cache` do) rather than relying on the
    repo's `.gitignore`: the first thing to create it can be an MCP query or `specky index` in a
    repo nobody has run `specky init` in, and an untracked `.specky/` appearing in `git status` there
    is specky leaving litter in someone else's tree.
    """
    path = repo_root / STATE_DIR_NAME
    path.mkdir(exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# Created by specky; everything here is derived or per-checkout.\n*\n")
    return path
