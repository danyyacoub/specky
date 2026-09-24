"""Which repo the MCP tools answer for, when the host starts the server somewhere else.

Devin Desktop starts its MCP servers from the user's home directory, before any session has picked
a workspace, so the cwd names no repo. These run the real server over stdio from outside any repo
and check each way it can still find one: the workspace roots the host reports, and
SPECKY_REPO_ROOT for a host that reports none.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from specky import mcp_server


def _call(cwd: Path, tool: str, roots: list[Path] | None = None, env: dict | None = None):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "specky.mcp_server"],
        cwd=cwd,
        env={**os.environ, **(env or {})},
    )

    async def list_roots(_ctx):
        return types.ListRootsResult(roots=[types.Root(uri=r.as_uri()) for r in roots or []])

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read,
                write,
                list_roots_callback=list_roots if roots is not None else None,
            ) as session:
                await session.initialize()
                return await session.call_tool(tool, {})

    return anyio.run(run)


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def outside(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("home")


@pytest.fixture
def repo_with_doc(tmp_repo: Path, write_doc) -> Path:
    write_doc("billing/refunds.md", "# Refunds\n")
    return tmp_repo


def test_the_hosts_workspace_root_names_the_repo(outside, repo_with_doc):
    result = _call(outside, "list_domains", roots=[repo_with_doc])
    assert not result.is_error, _text(result)
    assert "billing/refunds.md" in _text(result)


def test_specky_repo_root_names_the_repo(outside, repo_with_doc):
    result = _call(outside, "list_domains", env={"SPECKY_REPO_ROOT": str(repo_with_doc)})
    assert not result.is_error, _text(result)
    assert "billing/refunds.md" in _text(result)


def test_no_repo_anywhere_says_what_to_set(outside):
    result = _call(outside, "list_domains")
    assert result.is_error
    assert "SPECKY_REPO_ROOT" in _text(result)


def test_outside_a_repo_the_model_is_not_told_to_skip_the_tools():
    assert mcp_server.instructions_for(None) == mcp_server.UNKNOWN_REPO_INSTRUCTIONS
