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
    "specky indexes this repo's functional docs (specs/<domain>/<topic>.md, a domain being a "
    "module) and the history of why they changed. Check it first for behaviour questions: what a "
    "feature does, how a workflow runs, its rules, statuses and edge cases, whether something is "
    "supported, and why it changed. doc_context takes a topic or a doc path and returns the doc "
    "with its neighbours in the docs graph, its behaviours and its recent changes; search_docs and "
    "read_doc go wider. module_acceptance_tests lists a module's acceptance tests; doc_behaviours "
    "one doc's exact promises. search_history filters the changes by query, author, module and "
    "since (24h, 7d, an ISO date); recent_activity sums a window up by module. Cite the doc path. "
    "Docs state intended behaviour and can lag the code, so check the files in a doc's `sources` "
    "frontmatter before changing code on its word. Not for finding where a symbol or file lives "
    "in the code; use code search for that. search_docs, search_history's query and the catalog "
    "tools need `specky index` to have run."
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


# Set by `specky serve` when it serves these tools over HTTP (mcp_http.py): that process answers for
# one repo, the one it serves, whatever its cwd or the remote host's workspace happens to be.
_pinned_root: Path | None = None


def pin_repo(root: Path) -> None:
    """Answer every tool call for `root`, ahead of `SPECKY_REPO_ROOT`, the cwd and client roots."""
    global _pinned_root
    _pinned_root = root
    mcp._lowlevel_server.instructions = instructions_for(root)


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
    A server pinned by `pin_repo` skips all of that.
    """
    if _pinned_root is not None:
        return _pinned_root
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

def _answer(fn, *args, **kwargs):
    """`fn(*args, **kwargs)`, its `ValueError` (a bad path, module or `since`) as a `ToolError`, so
    the model reads the reason rather than the SDK's generic failure."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# `commits_for_doc` had no cap before it fell back to the history docs; this keeps it near enough.
COMMITS_FOR_DOC_MAX = 200

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
    return _answer(doc_tools.doc_history, await _repo(ctx), doc_path, limit=COMMITS_FOR_DOC_MAX)


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
async def search_history(
    ctx: Context,
    query: str = "",
    author: str = "",
    module: str = "",
    since: str = "",
    impact: str = "",
    limit: int = doc_tools.HISTORY_LIMIT,
) -> list[dict]:
    """The changes recorded in the docs' history, newest first (by relevance with a `query`): what
    used to be true, and why it changed. Every filter is optional, but give at least one:
    `query` (full text), `author` (part of a name or email), `module` (a domain like 'billing', or a
    doc path), `since` (a duration like '24h', '7d', '2w', or an ISO date) and `impact` (feature |
    improvement | fix | internal | breaking). E.g. since='24h' for the last day's changes, or
    author='dany', module='chat', since='7d'. Each row: path (the history doc, read it with
    read_doc), headline, date, authors, impact, docs it touched, what and why."""
    return _answer(
        doc_tools.search_history,
        await _repo(ctx),
        query,
        limit,
        author=author,
        module=module,
        since=since,
        impact=impact,
    )


@mcp.tool(annotations=READ_ONLY)
async def module_acceptance_tests(
    module: str, ctx: Context, include_edge_cases: bool = False
) -> list[dict]:
    """Every acceptance test a module's docs state, grouped by doc: [{path, title, tests: [{id,
    text, fields}]}]. `module` is a domain (a docs folder, e.g. 'billing' — list_domains names them)
    or one doc path. `include_edge_cases` adds the EDGE-n rows of each doc's Edge Cases table."""
    return _answer(
        doc_tools.module_acceptance_tests, await _repo(ctx), module, include_edge_cases
    )


@mcp.tool(annotations=READ_ONLY)
async def doc_context(topic: str, ctx: Context) -> dict:
    """The doc a topic is about, with what surrounds it — start here. `topic` is a doc path or a
    few words (the best search hit is taken; `alternatives` names the runners-up). Returns its
    frontmatter fields, content, behaviours by id (STEP/OUT/EDGE/AT), `neighbours` (docs linked by
    `related:` or a shared tag, with the reason) and its `recent` changes from the history."""
    return _answer(doc_tools.doc_context, await _repo(ctx), topic)


@mcp.tool(annotations=READ_ONLY)
async def recent_activity(ctx: Context, since: str = "7d", module: str = "") -> dict:
    """What changed in a window, by module, with counts per author and impact — the history docs
    summed up. `since` as search_history takes it ('24h', '7d', an ISO date); `module` narrows it
    to one domain or doc."""
    return _answer(doc_tools.recent_activity, await _repo(ctx), since, module)


@mcp.tool(annotations=READ_ONLY)
def render_acceptance_table(rows: list[dict]) -> str:
    """Approved acceptance-test rows ({scenario, given, when, then}) as the markdown table a doc's
    `## Acceptance Tests` section carries — the exact table the Spec Assistant splices into its own
    drafts, in the shape `specky tests` reads."""
    return spec_draft.acceptance_table(rows)


# --- resources: the docs as something a host can attach ------------------------------------------


def _static_root() -> Path:
    """The repo a static resource reads from. The SDK gives those no `Context`, so no client roots:
    the pinned repo on `specky serve`, else `SPECKY_REPO_ROOT` or the cwd as `_repo` has them."""
    root = _pinned_root or _startup_root()
    if root is None:
        raise RepoNotFound("specky-mcp can't tell which repo to read: set SPECKY_REPO_ROOT")
    return root


def _read_overview(path_for) -> str:
    root = _static_root()
    path = path_for(root)
    if not path.is_file():
        raise ToolError(f"{path.relative_to(root)} does not exist in this repo")
    return path.read_text()


@mcp.resource("specky://product", name="product", mime_type="text/markdown")
def product_resource() -> str:
    """What the product is and who it's for — the docs tree's PRODUCT.md."""
    return _read_overview(paths.product_doc)


@mcp.resource("specky://modules", name="modules", mime_type="text/markdown")
def modules_resource() -> str:
    """Every module (domain) and the docs in it, with one line each — the docs tree's MODULES.md."""
    return _read_overview(paths.modules_index)


@mcp.resource("specky://glossary", name="glossary", mime_type="text/markdown")
def glossary_resource() -> str:
    """The domain's own terms and what they mean — the docs tree's GLOSSARY.md."""
    return _read_overview(paths.glossary)


@mcp.resource("specky://doc/{domain}/{topic}", name="doc", mime_type="text/markdown")
async def doc_resource(domain: str, topic: str, ctx: Context) -> str:
    """One doc as `<domain>/<topic>.md` under the docs tree, e.g.
    specky://doc/billing/refund-flow.md."""
    return _answer(doc_tools.read_doc, await _repo(ctx), f"{unquote(domain)}/{unquote(topic)}")


@mcp.prompt(title="Explore the docs")
def explore(question: str) -> str:
    """Answer a question from this project's docs: a short answer first, then the details."""
    return (
        f"Answer this question from the project's docs: {question}\n\n"
        "Use the specky tool doc_context to find the doc it is about and what surrounds it, "
        "search_docs and read_doc for anything further, and search_history when the answer is about "
        "why something changed. Answer only from what "
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
