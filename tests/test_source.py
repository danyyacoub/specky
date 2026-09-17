"""Reading a repo's own source: what `source.py` will read, what it refuses, and what it hides.

No provider anywhere in this file. Every landmine here is a file on disk and an assertion — a
binary with a `.py` extension, a minified bundle, a symlink, a CP1252-saved module — and the
refusals matter more than the successes: each one is a real failure that aborted a run before the
guard existed.

The other half is the allowlist. `source_files` is not just an enumeration, it is the set every
path-taking tool resolves against (`tools.py`), so a hole in it is a model reading somewhere it
should not be able to reach.
"""

from __future__ import annotations

from pathlib import Path

from specky import source

from conftest import git


def _src(repo: Path, rel: str, body: str = "def hello():\n    return 1\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _commit(repo: Path, message: str = "add source") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


# --- what is visible at all -----------------------------------------------------------------------


def test_a_gitignored_file_is_invisible(tmp_repo):
    """The load-bearing property of enumerating with `git ls-files` rather than an rglob: a
    node_modules/ or .venv/ is excluded by construction, not by a denylist per ecosystem."""
    _src(tmp_repo, "src/real.py")
    _src(tmp_repo, "node_modules/pkg/index.js", "module.exports = 1;\n")
    (tmp_repo / ".gitignore").write_text("node_modules/\n")
    _commit(tmp_repo)

    files = source.source_files(tmp_repo)
    assert "src/real.py" in files
    assert not any("node_modules" in f for f in files)


def test_tracked_vendored_code_is_excluded(tmp_repo):
    """gitignore can't help here — a Go `vendor/` or a committed `node_modules/` is tracked, so it
    would otherwise be offered to a model as though it were this repo's own code."""
    _src(tmp_repo, "src/app.py")
    _src(tmp_repo, "vendor/dep/lib.py")
    _src(tmp_repo, "src/api_pb2.py")
    _src(tmp_repo, "web/bundle.min.js", "var a=1;\n")
    _commit(tmp_repo)

    assert source.source_files(tmp_repo) == ["src/app.py"]


def test_the_docs_tree_is_not_source(tmp_repo):
    """Or one generated doc's invention would ground the next one's."""
    _src(tmp_repo, "src/app.py")
    _src(tmp_repo, "specs/notes.py", "# not really source\n")
    _commit(tmp_repo)

    assert source.source_files(tmp_repo) == ["src/app.py"]


def test_scoping_excludes_everything_outside_the_subtree(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py")
    _src(tmp_repo, "src/auth/login.py")
    _commit(tmp_repo)

    assert source.source_files(tmp_repo, scope="src/billing") == ["src/billing/refund.py"]


# --- what read_source refuses ---------------------------------------------------------------------


def test_a_cp1252_source_file_does_not_abort_the_walk(tmp_repo):
    """Git hands back a non-UTF-8 file with no early NUL as text, and a strict decode would abort
    the whole run over one file — the landmine `commit_doc._commit_info` already documents."""
    path = _src(tmp_repo, "src/cafe.py")
    path.write_bytes(b"# caf\xe9\ndef order():\n    return 1\n")
    _commit(tmp_repo)

    assert "�" in source.read_source(path)


def test_a_binary_file_with_a_source_extension_is_skipped(tmp_repo):
    path = _src(tmp_repo, "src/blob.py")
    path.write_bytes(b"\x89PNG\x00\x00\x00\rIHDR" + b"\xff" * 200)
    _commit(tmp_repo)

    assert source.read_source(path) is None


def test_a_minified_bundle_is_never_read(tmp_repo):
    path = _src(tmp_repo, "src/vendor.js", "var a=1;" * 40_000)  # one enormous line
    _commit(tmp_repo)

    assert source.read_source(path) is None


def test_an_oversized_file_is_never_read(tmp_repo):
    path = _src(tmp_repo, "src/big.py", "# padding\n" * (source.MAX_FILE_BYTES // 5))
    _commit(tmp_repo)

    assert source.read_source(path) is None


def test_a_symlink_is_never_read_as_source(tmp_repo):
    """`git ls-files` lists symlinks, and one can point anywhere — including out of the repo."""
    _src(tmp_repo, "src/real.py")
    link = tmp_repo / "src" / "alias.py"
    link.symlink_to("real.py")
    _commit(tmp_repo)

    assert source.read_source(link) is None


# --- outlines -------------------------------------------------------------------------------------


def test_python_symbols_come_from_ast_and_skip_private_names(tmp_repo):
    text = '"""What this module is."""\n\ndef public():\n    pass\n\ndef _private():\n    pass\n'
    summary = source.file_summary("src/a.py", text)
    assert "What this module is." in summary
    assert "public" in summary and "_private" not in summary


def test_an_unparseable_python_file_still_yields_a_header(tmp_repo):
    """A py2 module or a template with `.py` on it must not raise out of an outline call."""
    summary = source.file_summary("src/old.py", "# A legacy module.\nprint 'hi'\n")
    assert "A legacy module." in summary


def test_symbols_are_found_in_a_language_with_no_stdlib_parser(tmp_repo):
    text = "// Billing helpers.\nexport function refundOrder() {}\nexport class Ledger {}\n"
    summary = source.file_summary("src/billing.ts", text)
    assert "refundOrder" in summary and "Ledger" in summary


def test_tests_and_entry_points_rank_where_they_should():
    """Entry points first because they name what a directory exports; tests last because a doc
    written from a test suite describes the fixtures rather than the feature."""
    ordered = sorted(
        ["pkg/zebra.py", "tests/test_billing.py", "pkg/__init__.py", "pkg/alpha.py"],
        key=source.rank,
    )
    assert ordered[0] == "pkg/__init__.py"
    assert ordered[-1] == "tests/test_billing.py"


def test_clip_announces_its_own_cut():
    """A silent truncation reads to a model as a file that simply ends there, and it will document
    the half it was shown as the whole thing."""
    assert "…[truncated —" in source.clip("x" * 100, 10)
    assert source.clip("short", 10) == "short"


# --- the repo's shape -----------------------------------------------------------------------------


def test_the_tree_names_every_directory_holding_source(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py")
    _src(tmp_repo, "src/auth/login.py")
    _commit(tmp_repo)

    tree = source.tree(tmp_repo)
    assert "src/billing/" in tree and "src/auth/" in tree
    assert "1 files" in tree


def _grep(repo, pattern, **kw):
    """`grep` takes its allowlist explicitly; these tests search everything tracked."""
    return source.grep(repo, pattern, allowed=set(source.source_files(repo)), **kw)


def test_grep_finds_a_hit_and_reports_where(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py", "def refund_order():\n    return 'refunded'\n")
    _commit(tmp_repo)

    hits = _grep(tmp_repo, "refund_order")
    assert hits and hits[0].startswith("src/billing/refund.py:1:")


def test_grep_is_case_insensitive_and_can_take_a_regex(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py", "def RefundOrder():\n    pass\n")
    _commit(tmp_repo)

    assert _grep(tmp_repo, "refundorder")
    assert _grep(tmp_repo, r"def\s+Refund\w+", regex=True)


def test_grep_never_returns_a_path_outside_the_allowlist(tmp_repo):
    """git grep honours .gitignore but knows nothing about vendored trees or the docs root, so the
    filter afterwards is what actually contains a search."""
    _src(tmp_repo, "src/app.py", "SECRET_TERM = 1\n")
    _src(tmp_repo, "vendor/dep/lib.py", "SECRET_TERM = 2\n")
    _src(tmp_repo, "specs/leak.py", "SECRET_TERM = 3\n")
    _commit(tmp_repo)

    hits = _grep(tmp_repo, "SECRET_TERM")
    assert [h.split(":")[0] for h in hits] == ["src/app.py"]


def test_grep_returns_only_what_the_caller_allowed(tmp_repo):
    """The allowlist is the caller's, not a pathspec's — which is what lets a scoped run still
    reach the tests for its subject while everything else stays out of view."""
    _src(tmp_repo, "src/billing/refund.py", "SHARED = 1\n")
    _src(tmp_repo, "src/auth/login.py", "SHARED = 2\n")
    _commit(tmp_repo)

    hits = source.grep(tmp_repo, "SHARED", allowed={"src/billing/refund.py"})

    assert [h.split(":")[0] for h in hits] == ["src/billing/refund.py"]


def test_grep_stops_at_its_limit(tmp_repo):
    _src(tmp_repo, "src/many.py", "".join(f"needle_{i} = {i}\n" for i in range(100)))
    _commit(tmp_repo)

    assert len(_grep(tmp_repo, "needle_", limit=5)) == 5


def test_grep_with_no_matches_is_an_answer_not_an_error(tmp_repo):
    """git grep exits 1 for 'nothing found', which `check=True` would turn into a crash."""
    _src(tmp_repo, "src/app.py")
    _commit(tmp_repo)

    assert _grep(tmp_repo, "nothing_here_at_all") == []
