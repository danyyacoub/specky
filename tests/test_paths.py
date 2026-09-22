"""The configurable docs root.

Two things to pin down. First the precedence, which exists for one reason: `specky init` writes a
*gitignored* specky.toml, so a root recorded only there is invisible in CI — where `specky check`
and `specky index` must resolve the same tree or the gate silently reads an empty one. pyproject.toml
is the committed answer.

Second, and the part that would actually break: a repo whose root is `documentation/` has to work
end to end. `"specs"` used to be spelled out at ~25 sites, and any single one left behind is a
module quietly reading a directory that doesn't exist — an index with no docs, a `check` that
enforces nothing, a viewer with an empty sidebar. So the end-to-end test drives index → search →
check → render → export on one such repo rather than unit-testing each call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from specky import paths
from specky.paths import DEFAULT_DOCS_ROOT, DocsConfig

from conftest import git


@pytest.fixture(autouse=True)
def _clear_paths_cache():
    """`_config` is `lru_cache`d per repo root, and these tests rewrite the config mid-process."""
    paths.reset_cache()
    yield
    paths.reset_cache()


class TestPrecedence:
    def test_defaults_to_specs(self, tmp_repo: Path):
        assert DocsConfig.load(tmp_repo).root == DEFAULT_DOCS_ROOT
        assert paths.docs_root(tmp_repo) == tmp_repo / "specs"
        assert paths.docs_prefix(tmp_repo) == "specs/"
        assert paths.history_dir(tmp_repo) == tmp_repo / "specs" / "history"
        assert paths.history_prefix(tmp_repo) == "specs/history/"
        assert paths.modules_index(tmp_repo) == tmp_repo / "specs" / "MODULES.md"

    def test_reads_pyproject_so_ci_can_see_it(self, tmp_repo: Path):
        (tmp_repo / "pyproject.toml").write_text('[tool.specky.docs]\nroot = "documentation"\n')
        assert DocsConfig.load(tmp_repo).root == "documentation"

    def test_specky_toml_wins_over_pyproject(self, tmp_repo: Path):
        (tmp_repo / "pyproject.toml").write_text('[tool.specky.docs]\nroot = "documentation"\n')
        (tmp_repo / "specky.toml").write_text('[docs]\nroot = "local-docs"\n')
        assert DocsConfig.load(tmp_repo).root == "local-docs"

    def test_a_specky_toml_without_a_docs_table_falls_through(self, tmp_repo: Path):
        # Every specky.toml has an `[ai]` table and most have no `[docs]` one, so "the file exists"
        # can't be what shadows pyproject — only a `[docs]` table can.
        (tmp_repo / "pyproject.toml").write_text('[tool.specky.docs]\nroot = "documentation"\n')
        (tmp_repo / "specky.toml").write_text('[ai]\nprovider = "anthropic"\n')
        assert DocsConfig.load(tmp_repo).root == "documentation"

    def test_trailing_slashes_and_whitespace_are_trimmed(self, tmp_repo: Path):
        (tmp_repo / "specky.toml").write_text('[docs]\nroot = "  documentation/  "\n')
        assert DocsConfig.load(tmp_repo).root == "documentation"

    @pytest.mark.parametrize("root", ["/etc/specs", "../outside", "docs/../../outside", ""])
    def test_a_root_outside_the_repo_falls_back_to_the_default(self, tmp_repo: Path, root: str):
        # Every consumer assumes docs live inside the repo: paths are stored in the index relative
        # to the root and handed to git as pathspecs, neither of which survives an escape.
        (tmp_repo / "specky.toml").write_text(f'[docs]\nroot = "{root}"\n')
        assert DocsConfig.load(tmp_repo).root == DEFAULT_DOCS_ROOT

    def test_the_cache_is_per_repo_root(self, tmp_repo: Path, tmp_path: Path):
        other = tmp_path / "other"
        other.mkdir()
        (other / "specky.toml").write_text('[docs]\nroot = "documentation"\n')
        assert paths.docs_root(tmp_repo).name == "specs"
        assert paths.docs_root(other).name == "documentation"


DOC = "documentation/billing/refund-flow.md"
DOC_BODY = (
    "---\ntype: feature\ntags: [billing]\n---\n\n"
    "# Billing — Refund Flow\n\nHow a refund runs, in `src/refunds.py`.\n"
)


@pytest.fixture
def custom_root_repo(tmp_repo: Path) -> Path:
    """A repo that keeps its docs in `documentation/`, documented the way the hook does it.

    Two documented commits, not one: `check.MIN_LINK_COMMITS` needs a file and a doc to have moved
    together twice before it will enforce the link, so a single pair proves nothing to `check`.
    """
    (tmp_repo / "pyproject.toml").write_text('[tool.specky.docs]\nroot = "documentation"\n')
    (tmp_repo / "specs").rmdir()
    paths.reset_cache()
    (tmp_repo / "src").mkdir()

    for i in range(2):
        (tmp_repo / "src/refunds.py").write_text(f"def refund{i}(): ...\n")
        git(tmp_repo, "add", "-A")
        git(tmp_repo, "commit", "-q", "-m", f"work on the refund path {i}")
        sha = git(tmp_repo, "rev-parse", "HEAD").strip()

        docs = tmp_repo / "documentation"
        (docs / "billing").mkdir(parents=True, exist_ok=True)
        # The body has to change each round: git records a commit as touching a file only if the
        # content moved, so an identical rewrite is no link at all.
        (tmp_repo / DOC).write_text(f"{DOC_BODY}\nRevision {i}.\n")
        (docs / "history").mkdir(exist_ok=True)
        (docs / f"history/{sha[:8]}.md").write_text(f"---\nsha: {sha}\n---\n\n# Commit {sha[:8]}\n")
        (docs / "MODULES.md").write_text(
            "# Modules\n\n## Billing\n\n| Doc | Purpose |\n|---|---|\n"
            "| [billing/refund-flow.md](billing/refund-flow.md) | Issue refunds |\n"
        )
        git(tmp_repo, "add", "-A")
        git(tmp_repo, "commit", "-q", "-m", "docs: sync specky docs [skip specky]")
    return tmp_repo


class TestCustomRootEndToEnd:
    def test_index_reads_the_configured_tree(self, custom_root_repo: Path):
        from specky.indexer import run_index, search

        doc_count, commit_count = run_index(custom_root_repo)
        assert doc_count == 4  # the feature doc, two history docs, MODULES.md
        assert commit_count >= 4

        hits = search(custom_root_repo, "refund")
        assert any(hit["path"] == DOC for hit in hits)

    def test_check_enforces_docs_from_the_configured_tree(self, custom_root_repo: Path):
        from specky.check import run_check
        from specky.indexer import run_index

        # The history docs naming src/refunds.py are what build the link, so a later commit touching
        # that file without touching the doc is the violation `check` exists to catch.
        (custom_root_repo / "src/refunds.py").write_text("def refund(amount): ...\n")
        git(custom_root_repo, "commit", "-qam", "change the refund signature")
        run_index(custom_root_repo)

        report = run_check(custom_root_repo, base="HEAD~1")
        assert [v.doc_path for v in report.violations] == [DOC]

    def test_the_viewer_renders_from_the_configured_tree(self, custom_root_repo: Path):
        from specky.html_render import render_site
        from specky.indexer import run_index

        run_index(custom_root_repo)
        index_html = render_site(custom_root_repo)

        assert "Refund Flow" in index_html.read_text()
        # The docs-root segment is dropped from the page slug, whatever the root is called — so the
        # page is named after the doc, not after the directory it happens to live in.
        assert (index_html.parent / "billing-refund-flow.html").exists()

    def test_export_reads_the_configured_tree(self, custom_root_repo: Path):
        from specky.export import run_export
        from specky.indexer import run_index

        run_index(custom_root_repo)
        result = run_export(custom_root_repo)

        text = result.written[0].read_text()
        assert "Refund Flow" in text
        # `history/` is left out by default — one doc per commit would swamp a handable file.
        assert result.history_skipped == 2

    def test_doctor_looks_for_the_configured_tree(self, custom_root_repo: Path, monkeypatch):
        from specky.doctor import run_checks

        monkeypatch.chdir(custom_root_repo)
        backlog = [c for c in run_checks() if "history doc" in c.detail]

        # 1 of 3: only the fixture's own `initial commit` is undocumented. A doctor still looking in
        # `specs/` would find no history docs at all and call all three a backlog.
        assert [c.detail.split(" — ")[0] for c in backlog] == [
            "1 of the last 3 documentable commits have no history doc"
        ]

    def test_test_scaffolds_are_named_after_the_doc_not_the_root(self):
        from specky.testgen import out_path_for

        assert out_path_for(DOC) == "tests/spec/test_billing_refund_flow.py"

    def test_the_cli_reports_the_configured_tree(self, custom_root_repo: Path, monkeypatch, capsys):
        from specky import cli
        from specky.indexer import run_index

        run_index(custom_root_repo)
        monkeypatch.chdir(custom_root_repo)
        monkeypatch.setattr("sys.argv", ["specky", "check", "--base", "HEAD~1", "--json"])
        cli.main()

        report = json.loads(capsys.readouterr().out)
        assert DOC in json.dumps(report)


class TestStateDir:
    """`.specky/` has to stay out of `git status` in a repo nobody ran `specky init` in: the plugin's
    MCP server and `specky index` can be the first things to create it there."""

    def test_ignores_itself(self, tmp_repo: Path):
        state = paths.state_dir(tmp_repo)

        assert state == tmp_repo / ".specky"
        assert (state / ".gitignore").read_text().splitlines()[-1] == "*"

    def test_an_index_leaves_the_tree_clean(self, tmp_repo: Path):
        from specky.indexer import run_index

        # tmp_repo has no .gitignore at all, so nothing but `.specky/.gitignore` can be hiding it.
        assert not (tmp_repo / ".gitignore").exists()
        run_index(tmp_repo)

        assert (tmp_repo / ".specky" / "index.db").exists()
        assert git(tmp_repo, "status", "--porcelain") == ""

    def test_leaves_an_existing_ignore_file_alone(self, tmp_repo: Path):
        (tmp_repo / ".specky").mkdir()
        (tmp_repo / ".specky" / ".gitignore").write_text("index.db\n")

        paths.state_dir(tmp_repo)

        assert (tmp_repo / ".specky" / ".gitignore").read_text() == "index.db\n"
