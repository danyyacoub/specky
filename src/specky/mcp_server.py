"""MCP server exposing specky's tools over stdio.

Two groups. The catalog tools (`list_features` … `commits_for_doc`) answer questions about the
feature/workflow graph. The docs tools (`list_domains` … `render_acceptance_table`) are thin
wrappers over `doc_tools.py` — the read-only functions whose toolbox the viewer's Spec Assistant
hands its own model — plus the Spec Assistant's own acceptance-table renderer, so an agent host can
run the Spec Assistant's workflows with *its* model. The `explore` and `draft_spec` prompts spell
those workflows out, built from the same rule text the panel's prompts are (`spec_draft.py`), so the
two can't drift.
"""

from mcp.server.mcpserver import MCPServer

from specky import catalog, doc_tools, spec_draft
from specky.chat_server import EXPLORE_FORMAT
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


# --- the Spec Assistant's docs tools --------------------------------------------------------------


@mcp.tool()
def list_domains() -> list[dict]:
    """Every domain (folder) of the docs tree with the docs in it: path, title, type (feature |
    workflow) and one-line purpose. Read off disk, so it is current even before `specky index`."""
    return doc_tools.list_domains(repo_root())


@mcp.tool()
def search_docs(
    query: str, domain: str = "", limit: int = doc_tools.SEARCH_LIMIT_DEFAULT
) -> list[dict]:
    """Full-text search over the docs (history docs excluded), ranked the way the Spec Assistant
    ranks them. Pass `domain` to search one folder only. Requires `specky index`."""
    return doc_tools.search_docs(repo_root(), query, domain or None, limit)


@mcp.tool()
def read_doc(path: str) -> str:
    """One doc's full text, frontmatter included — e.g. 'specs/chat/local-rag-server.md'. Only
    paths inside the docs tree are readable."""
    return doc_tools.read_doc(repo_root(), path)


@mcp.tool()
def doc_behaviours(path: str) -> list[dict]:
    """The behaviours one doc states, each with a stable id: STEP-n (How It Works), OUT-n
    (Outcomes), EDGE-n (Edge Cases), AT-n (Acceptance Tests). Each row is {id, section, text,
    fields}. Use the ids to say exactly which promise a change alters."""
    return doc_tools.doc_behaviours(repo_root(), path)


@mcp.tool()
def search_history(query: str) -> list[dict]:
    """Commits whose message or summary matches `query` — what used to be true, and why it
    changed. Requires `specky index`."""
    return doc_tools.search_history(repo_root(), query)


@mcp.tool()
def render_acceptance_table(rows: list[dict]) -> str:
    """Approved acceptance-test rows ({scenario, given, when, then}) as the markdown table a doc's
    `## Acceptance Tests` section carries — the exact table the Spec Assistant splices into its own
    drafts, in the shape `specky tests` reads."""
    return spec_draft.acceptance_table(rows)


@mcp.prompt(title="Explore the docs")
def explore(question: str) -> str:
    """Answer a question from this project's docs: a short answer first, then the details."""
    return (
        f"Answer this question from the project's docs: {question}\n\n"
        "Use the specky tools search_docs and read_doc to find and read the docs it is about, and "
        "search_history when the answer is about why something changed. Answer only from what "
        f"they say, citing the doc path each part comes from.\n\n{EXPLORE_FORMAT}"
    )


@mcp.prompt(title="Draft a spec change")
def draft_spec(request: str) -> str:
    """Draft a spec change with the reader: scope, impact, approved acceptance tests, final doc."""
    return spec_draft.workflow_prompt(request)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
