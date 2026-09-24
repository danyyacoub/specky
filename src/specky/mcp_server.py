"""MCP server exposing specky's tools over stdio.

Two groups. The catalog tools (`list_features` … `commits_for_doc`) answer questions about the
feature/workflow graph. The docs tools (`list_domains` … `render_acceptance_table`) are thin
wrappers over `doc_tools.py` — the read-only functions whose toolbox the viewer's Spec Assistant
hands its own model — plus the Spec Assistant's own acceptance-table renderer, so an agent host can
run the Spec Assistant's workflows with *its* model. The `explore` and `draft_spec` prompts spell
those workflows out, built from the same rule text the panel's prompts are (`spec_draft.py`), so the
two can't drift.
"""

import os
import subprocess
import warnings
from pathlib import Path
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from specky import __version__, catalog, doc_tools, paths, spec_draft
from specky.chat_server import EXPLORE_FORMAT
from specky.db import repo_root

# Sent once at connect time and shown to the host's model. Without it, hosts that defer MCP tools
# (Claude Code) list only their names, and nothing tells the model a behaviour question has a
# cheaper, citable answer here than a read through the source.
INSTRUCTIONS = (
    "specky indexes this repo's functional docs (specs/<domain>/<topic>.md) and its git history. "
    "Check it first for behaviour questions: what a feature does, how a workflow runs, its rules, "
    "statuses and edge cases, whether something is supported, and why it changed. Use "
    "search_docs then read_doc; doc_behaviours for the exact promises a doc makes (its "
    "acceptance tests); search_history or commits_for_doc for why. Cite the doc path. Docs state "
    "intended behaviour and can lag the code, so check the files in a doc's `sources` "
    "frontmatter before changing code on its word. Not for finding where a symbol or file lives "
    "in the code; use code search for that. search_docs, search_history and the catalog tools "
    "need `specky index` to have run."
)

# The plugin is enabled per user, so the server starts in every repo the user opens, most of which
# have never heard of specky. Telling the model to "check it first" there sends it on a round trip
# that can only come back empty, every time a behaviour question comes up.
NO_DOCS_INSTRUCTIONS = (
    "specky is installed, but this repo has no specky docs yet, so its tools have nothing to answer "
    "from. Don't call them unless the user asks about specky; the /specky:setup skill sets a repo "
    "up."
)


# Some hosts start their MCP servers from a directory that isn't the project — Devin Desktop starts
# them from the user's home, before any session has picked a workspace. Which repo the tools answer
# for is then only known per call (see `_repo`), so the model is told what to do either way.
UNKNOWN_REPO_INSTRUCTIONS = (
    "specky answers questions from a repo's functional docs and git history. If the workspace "
    "has specky docs (a specky.toml, or a specs/<domain>/<topic>.md tree), check it first for "
    "behaviour questions: search_docs then read_doc, citing the doc path. Otherwise leave these "
    "tools alone."
)


def instructions_for(root: Path | None) -> str:
    """The instructions for a server started in `root` (None: not inside a git repo).

    Any markdown under the docs root counts as docs: a `specs/` holding only OpenAPI files is
    somebody else's tree, not specky's.
    """
    if root is None:
        return UNKNOWN_REPO_INSTRUCTIONS
    docs = paths.docs_root(root)
    if docs.is_dir() and next(docs.rglob("*.md"), None) is not None:
        return INSTRUCTIONS
    return NO_DOCS_INSTRUCTIONS


def _git_root(start: Path | None = None) -> Path | None:
    """The top of the git repo containing `start` (default: the cwd), or None."""
    try:
        if start is None:
            return repo_root()
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return None


def _startup_root() -> Path | None:
    override = os.environ.get("SPECKY_REPO_ROOT")
    return _git_root(Path(override)) if override else _git_root()


class RepoNotFound(ToolError):
    """A `ToolError`, so its message reaches the model instead of the SDK's generic one."""


async def _client_roots(ctx: Context) -> list[Path]:
    """The workspace folders the host says it has open, as local paths. [] if it won't say."""
    session = ctx.session
    caps = session.client_capabilities
    if caps is None or caps.roots is None:
        return []
    try:
        with warnings.catch_warnings():
            # Deprecated in the 2026-07-28 spec, still what today's hosts answer.
            warnings.simplefilter("ignore")
            result = await session.list_roots()
    except Exception:
        return []
    found = []
    for root in result.roots:
        uri = urlparse(str(root.uri))
        if uri.scheme == "file":
            found.append(Path(unquote(uri.path)))
    return found


async def _repo(ctx: Context) -> Path:
    """The repo a tool call answers for.

    `SPECKY_REPO_ROOT` wins, for a host that can only pass env. Then the cwd, which is right
    wherever the host starts its servers in the project (Claude Code, Codex, Kiro). Then the
    workspace roots the host reports — the only signal left when it starts them somewhere else.
    Resolved per call rather than once, since one server process can outlive a workspace switch.
    """
    override = os.environ.get("SPECKY_REPO_ROOT")
    if override:
        root = _git_root(Path(override).expanduser())
        if root is None:
            raise RepoNotFound(f"SPECKY_REPO_ROOT={override} is not inside a git repo")
        return root
    root = _git_root()
    if root is not None:
        return root
    candidates = [r for r in map(_git_root, await _client_roots(ctx)) if r is not None]
    # A multi-root workspace: prefer the repo that actually has specky set up.
    for candidate in candidates:
        if (candidate / "specky.toml").exists() or paths.docs_root(candidate).is_dir():
            return candidate
    if candidates:
        return candidates[0]
    raise RepoNotFound(
        f"specky-mcp was started outside a git repo (cwd: {Path.cwd()}) and the host reported no "
        "workspace folder that is one. Set SPECKY_REPO_ROOT to the repo's path in this server's "
        "`env`, or start the host from inside the repo."
    )


mcp = MCPServer(
    "specky",
    instructions=instructions_for(_startup_root()),
    # Which copy of specky answered: the plugin runs its own, pinned to the plugin's version,
    # separate from the `uv tool install` the git hooks call.
    version=__version__,
    website_url="https://github.com/danyyacoub/specky",
)

# Every tool only reads the docs tree, the index or git. Saying so lets clients that gate
# tools by mode (plan mode, read-only agents) still call them.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
def ping() -> str:
    """Health-check tool used to verify the specky MCP server is wired up correctly."""
    return "pong"


@mcp.tool(annotations=READ_ONLY)
async def list_features(ctx: Context) -> list[dict]:
    """List all feature docs (path, title, domain, tags). Requires `specky index` to have
    run at least once."""
    return catalog.list_features(await _repo(ctx))


@mcp.tool(annotations=READ_ONLY)
async def list_workflows(ctx: Context) -> list[dict]:
    """List all workflow docs (path, title, domain, tags). Requires `specky index` to have
    run at least once."""
    return catalog.list_workflows(await _repo(ctx))


@mcp.tool(annotations=READ_ONLY)
async def list_tags(ctx: Context) -> dict[str, list[dict]]:
    """Every tag in use, mapped to the docs carrying it."""
    return catalog.list_tags(await _repo(ctx))


@mcp.tool(annotations=READ_ONLY)
async def get_graph(ctx: Context) -> dict:
    """The feature/workflow graph as {nodes, edges} — an edge connects a workflow to a
    feature sharing a tag, or follows a doc's hand-authored `related` reference."""
    return catalog.build_graph(await _repo(ctx))


@mcp.tool(annotations=READ_ONLY)
async def commit_info(sha: str, ctx: Context) -> dict:
    """Tags and feature/workflow docs linked to a single commit."""
    return catalog.commit_info(await _repo(ctx), sha)


@mcp.tool(annotations=READ_ONLY)
async def commits_for_doc(doc_path: str, ctx: Context) -> list[dict]:
    """Commits linked to a given feature/workflow doc (path relative to the repo root,
    e.g. 'specs/billing/refund-flow.md'), most recent first — each with its history doc's one-line
    `headline`, its `impact` (feature | improvement | fix | internal) and `history_path`, read
    those with read_doc for what changed and why."""
    return catalog.commits_for_doc(await _repo(ctx), doc_path)


# --- the Spec Assistant's docs tools --------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def list_domains(ctx: Context) -> list[dict]:
    """Every domain (folder) of the docs tree with the docs in it: path, title, type (feature |
    workflow) and one-line purpose. Read off disk, so it is current even before `specky index`."""
    return doc_tools.list_domains(await _repo(ctx))


@mcp.tool(annotations=READ_ONLY)
async def search_docs(
    ctx: Context,
    query: str,
    domain: str = "",
    limit: int = doc_tools.SEARCH_LIMIT_DEFAULT,
) -> list[dict]:
    """Full-text search over the docs (history docs excluded), ranked the way the Spec Assistant
    ranks them. Pass `domain` to search one folder only. Requires `specky index`."""
    return doc_tools.search_docs(await _repo(ctx), query, domain or None, limit)


@mcp.tool(annotations=READ_ONLY)
async def read_doc(path: str, ctx: Context) -> str:
    """One doc's full text, frontmatter included — e.g. 'specs/chat/local-rag-server.md'. Only
    paths inside the docs tree are readable."""
    return doc_tools.read_doc(await _repo(ctx), path)


@mcp.tool(annotations=READ_ONLY)
async def doc_behaviours(path: str, ctx: Context) -> list[dict]:
    """The behaviours one doc states, each with a stable id: STEP-n (How It Works), OUT-n
    (Outcomes), EDGE-n (Edge Cases), AT-n (Acceptance Tests). Each row is {id, section, text,
    fields}. Use the ids to say exactly which promise a change alters."""
    return doc_tools.doc_behaviours(await _repo(ctx), path)


@mcp.tool(annotations=READ_ONLY)
async def search_history(query: str, ctx: Context) -> list[dict]:
    """Commits whose message or summary matches `query` — what used to be true, and why it
    changed. Requires `specky index`."""
    return doc_tools.search_history(await _repo(ctx), query)


@mcp.tool(annotations=READ_ONLY)
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
