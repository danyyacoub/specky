"""The instructions the MCP server hands the host's model at connect time.

The plugin is enabled per user, so the server starts in every repo the user opens. Only a repo
with docs should get "check specky first": anywhere else that instruction is a round trip that can
only come back empty, on every behaviour question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specky import mcp_server, paths


@pytest.fixture(autouse=True)
def _clear_paths_cache():
    paths.reset_cache()
    yield
    paths.reset_cache()


def test_a_repo_with_docs_gets_the_full_instructions(tmp_repo: Path, write_doc):
    write_doc("billing/refunds.md", "# Refunds\n")

    assert mcp_server.instructions_for(tmp_repo) == mcp_server.INSTRUCTIONS


def test_an_empty_docs_tree_counts_as_no_docs(tmp_repo: Path):
    # tmp_repo has a `specs/` directory with nothing in it.
    assert mcp_server.instructions_for(tmp_repo) == mcp_server.NO_DOCS_INSTRUCTIONS


def test_a_specs_dir_without_markdown_is_someone_elses(tmp_repo: Path):
    (tmp_repo / "specs" / "openapi.yaml").write_text("openapi: 3.1.0\n")

    assert mcp_server.instructions_for(tmp_repo) == mcp_server.NO_DOCS_INSTRUCTIONS


def test_a_repo_with_no_docs_root_is_told_to_leave_the_tools_alone(tmp_path: Path):
    assert mcp_server.instructions_for(tmp_path) == mcp_server.NO_DOCS_INSTRUCTIONS
    assert "/specky:setup" in mcp_server.NO_DOCS_INSTRUCTIONS


def test_outside_a_git_repo(tmp_path: Path):
    assert mcp_server.instructions_for(None) == mcp_server.UNKNOWN_REPO_INSTRUCTIONS
