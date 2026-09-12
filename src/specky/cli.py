"""`specky` console-script entry point: argument parsing, human-readable output, and
turning any failure into one clean stderr line instead of a traceback.

Two conventions hold across every command here:

- Handlers import what they need *inside* the function body. `specky commit-doc` runs on
  every commit in a repo with the hook installed, so nothing pays for a module (anthropic,
  markdown, jinja2, the MCP SDK) that this invocation isn't going to use.
- Handlers don't format their own errors. `main()` wraps the dispatch and prefixes whatever
  is raised with `specky <command>:`, so a handler signals a problem — including plain
  misuse, like `specky search` with no query — by raising.

The MCP server isn't a subcommand: it's the separate `specky-mcp` console script (see
pyproject.toml), which is what .mcp.json and the opencode/Kiro integrations point at.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _init(args: argparse.Namespace) -> None:
    from specky.setup_wizard import run_init

    run_init(Path("specky.toml"))


def _index(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.indexer import run_index

    doc_count, commit_count = run_index(repo_root())
    print(f"specky index: indexed {doc_count} docs, {commit_count} commits")


def _search(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.indexer import search as search_index

    if not args.query:
        raise ValueError('provide a query, e.g. `specky search "refund flow"`')
    hits = search_index(repo_root(), args.query)
    if not hits:
        print("specky search: no matches")
    for hit in hits:
        print(f"{hit['path']}  —  {hit['title']}")
        if hit["snippet"]:
            print(f"    {hit['snippet']}")


def _render_html(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.html_render import render_site

    print(f"specky render-html: wrote {render_site(repo_root())}")


def _serve(args: argparse.Namespace) -> None:
    from specky.chat_server import serve as run_serve
    from specky.db import repo_root

    # None, not a default: serve() falls back to `[serve]` in specky.toml before its own defaults.
    run_serve(repo_root(), port=args.port, host=args.host)


def _commit_doc(args: argparse.Namespace) -> None:
    from specky.commit_doc import main as commit_doc_main

    commit_doc_main()  # never raises by design — see its docstring


def _install_git_hook(args: argparse.Namespace) -> None:
    from specky.commit_doc import install_git_hook

    print(f"Installed {install_git_hook()}")


def _sync(args: argparse.Namespace) -> None:
    from specky.commit_doc import sync

    # sync() prints its own progress: one line per commit as it goes, so a backfill of several
    # hundred commits isn't a silent wait, plus a closing summary.
    sync(since=args.since, limit=args.limit, dry_run=args.dry_run, assume_yes=args.yes)


def _tag(args: argparse.Namespace) -> None:
    from specky.ai_provider import load_provider_from_toml
    from specky.db import repo_root
    from specky.generator import backfill_tags

    root = repo_root()
    updated = backfill_tags(root, load_provider_from_toml(root / "specky.toml"))
    if not updated:
        print("specky tag: already up to date")
    for path in updated:
        print(f"specky tag: tagged {path}")


def _list_docs(args: argparse.Namespace) -> None:
    """`features` and `workflows` — same output, different doc_type."""
    from specky import catalog
    from specky.db import repo_root

    lister = catalog.list_features if args.command == "features" else catalog.list_workflows
    docs = lister(repo_root())
    if not docs:
        print(f"specky {args.command}: none yet — run `specky tag` to classify existing docs")
    for doc in docs:
        tags = f" [{', '.join(doc['tags'])}]" if doc["tags"] else ""
        print(f"{doc['path']}  —  {doc['title']}{tags}  ({doc['commits']} commits)")


def _tags(args: argparse.Namespace) -> None:
    from specky import catalog
    from specky.db import repo_root

    by_tag = catalog.list_tags(repo_root())
    if not by_tag:
        print("specky tags: none yet — run `specky tag` to classify existing docs")
    for tag, docs in by_tag.items():
        print(f"{tag} ({len(docs)})")
        for doc in docs:
            print(f"    {doc['path']}  —  {doc['title']}")


def _graph(args: argparse.Namespace) -> None:
    from specky import catalog
    from specky.db import repo_root

    print(f"```mermaid\n{catalog.to_mermaid(catalog.build_graph(repo_root()))}\n```")


def _commit_info(args: argparse.Namespace) -> None:
    from specky import catalog
    from specky.db import repo_root

    info = catalog.commit_info(repo_root(), args.sha)
    if not info["docs"]:
        print(f"specky commit-info: no feature/workflow doc linked to {args.sha}")
        return
    print(f"tags: {', '.join(info['tags']) or '(none)'}")
    for doc in info["docs"]:
        print(f"{doc['path']}  —  {doc['title']} ({doc['type']})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specky")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, func) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(func=func)
        return sub

    command("init", "Choose/configure an AI provider, writes specky.toml", _init)
    command("index", "Index specs/ and git log into SQLite", _index)
    command("search", "Keyword search over the index", _search).add_argument("query", nargs="?")
    command("render-html", "Render the static HTML doc site", _render_html)
    serve_cmd = command("serve", "Serve the HTML viewer and its AI chat companion", _serve)
    serve_cmd.add_argument("--port", type=int, default=None)
    serve_cmd.add_argument(
        "--host",
        default=None,
        help="Bind address (default 127.0.0.1, or [serve] host). Anything but loopback exposes "
        "this repo's docs to whoever can reach the port",
    )
    command(
        "commit-doc",
        "Record a micro-doc for HEAD (invoked by the post-commit git hook)",
        _commit_doc,
    )
    command("install-git-hook", "Install the post-commit micro-doc hook", _install_git_hook)
    sync_cmd = command(
        "sync",
        "Backfill micro-docs for any commit that doesn't have one yet (idempotent)",
        _sync,
    )
    sync_cmd.add_argument(
        "--since", metavar="REV|DATE", help='e.g. v1.2.0, HEAD~50, or "2 weeks ago"'
    )
    sync_cmd.add_argument("--limit", type=int, metavar="N", help="Process at most N commits")
    sync_cmd.add_argument(
        "--dry-run", action="store_true", help="List the commits it would process, call nothing"
    )
    sync_cmd.add_argument(
        "--yes", action="store_true", help="Skip the confirmation for a large backfill"
    )
    command(
        "tag",
        "Backfill type/tags frontmatter for docs written before this feature existed",
        _tag,
    )
    command("features", "List feature docs", _list_docs)
    command("workflows", "List workflow docs", _list_docs)
    command("tags", "List tags and the docs carrying them", _tags)
    command("graph", "Print the feature/workflow graph as Mermaid", _graph)
    command(
        "commit-info", "Show tags and related feature/workflow docs for a commit", _commit_info
    ).add_argument("sha")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as exc:  # provider/network/config/git failures, and handler misuse
        print(f"specky {args.command}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
