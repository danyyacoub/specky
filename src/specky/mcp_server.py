"""MCP server exposing specky's tools over stdio."""

from mcp.server.mcpserver import MCPServer

from specky import catalog
from specky.db import repo_root

mcp = MCPServer("specky")


@mcp.tool()
def ping() -> str:
    """Health-check tool used to verify the specky MCP server is wired up correctly."""
    return "pong"


@mcp.tool()
def list_features() -> list[dict]:
    """List all feature docs (path, title, domain, tags). Requires `specky index` to have
    run at least once."""
    return catalog.list_features(repo_root())


@mcp.tool()
def list_workflows() -> list[dict]:
    """List all workflow docs (path, title, domain, tags). Requires `specky index` to have
    run at least once."""
    return catalog.list_workflows(repo_root())


@mcp.tool()
def list_tags() -> dict[str, list[dict]]:
    """Every tag in use, mapped to the docs carrying it."""
    return catalog.list_tags(repo_root())


@mcp.tool()
def get_graph() -> dict:
    """The feature/workflow graph as {nodes, edges} — an edge connects a workflow to a
    feature sharing a tag, or follows a doc's hand-authored `related` reference."""
    return catalog.build_graph(repo_root())


@mcp.tool()
def commit_info(sha: str) -> dict:
    """Tags and feature/workflow docs linked to a single commit."""
    return catalog.commit_info(repo_root(), sha)


@mcp.tool()
def commits_for_doc(doc_path: str) -> list[dict]:
    """Commits linked to a given feature/workflow doc (path relative to the repo root,
    e.g. 'specs/billing/refund-flow.md'), most recent first."""
    return catalog.commits_for_doc(repo_root(), doc_path)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
