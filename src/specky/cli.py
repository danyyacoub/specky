"""`specky` console-script entry point. Subcommands are filled in as their phase lands."""

import argparse
import sys
from pathlib import Path

_NOT_YET_IMPLEMENTED = {
    "generate": "performed by the document-domain skill directly, not scheduled as a CLI command",
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="specky")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Choose/configure an AI provider, writes specky.toml")
    subparsers.add_parser("index", help="Index specs/ and git log into SQLite")
    generate = subparsers.add_parser("generate", help="Generate/update a domain doc (Phase 2)")
    generate.add_argument("--domain", required=False)
    search = subparsers.add_parser("search", help="Keyword search over the index")
    search.add_argument("query", nargs="?")
    subparsers.add_parser("render-html", help="Render the static HTML doc site")
    serve = subparsers.add_parser("serve", help="Run the local AI chat companion for the HTML viewer")
    serve.add_argument("--port", type=int, default=None)
    subparsers.add_parser("mcp", help="Run the MCP server over stdio")
    subparsers.add_parser(
        "commit-doc", help="Record a micro-doc for HEAD (invoked by the post-commit git hook)"
    )
    subparsers.add_parser("install-git-hook", help="Install the post-commit micro-doc hook")
    subparsers.add_parser(
        "sync", help="Backfill micro-docs for any commit that doesn't have one yet (idempotent)"
    )
    subparsers.add_parser("features", help="List feature docs")
    subparsers.add_parser("workflows", help="List workflow docs")
    subparsers.add_parser("tags", help="List tags and the docs carrying them")
    subparsers.add_parser("graph", help="Print the feature/workflow graph as Mermaid")
    subparsers.add_parser(
        "tag", help="Backfill type/tags frontmatter for docs written before this feature existed"
    )
    commit_info_p = subparsers.add_parser(
        "commit-info", help="Show tags and related feature/workflow docs for a commit"
    )
    commit_info_p.add_argument("sha")

    args = parser.parse_args()

    if args.command == "mcp":
        from specky.mcp_server import main as mcp_main

        mcp_main()
        return

    if args.command == "init":
        from specky.setup_wizard import run_init

        try:
            run_init(Path("specky.toml"))
        except Exception as exc:  # surface provider/network/config errors cleanly, not a traceback
            print(f"specky init: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "commit-doc":
        from specky.commit_doc import main as commit_doc_main

        commit_doc_main()
        return

    if args.command == "install-git-hook":
        from specky.commit_doc import install_git_hook

        try:
            hook_path = install_git_hook()
        except RuntimeError as exc:
            print(f"specky install-git-hook: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Installed {hook_path}")
        return

    if args.command == "sync":
        from specky.commit_doc import sync

        try:
            written = sync()
        except Exception as exc:
            print(f"specky sync: {exc}", file=sys.stderr)
            sys.exit(1)
        if not written:
            print("specky sync: already up to date")
        else:
            for path in written:
                print(f"specky sync: wrote {path}")
        return

    if args.command == "index":
        from specky.db import repo_root
        from specky.indexer import run_index

        try:
            doc_count, commit_count = run_index(repo_root())
        except Exception as exc:
            print(f"specky index: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"specky index: indexed {doc_count} docs, {commit_count} commits")
        return

    if args.command == "search":
        from specky.db import repo_root
        from specky.indexer import search as search_index

        if not args.query:
            print('specky search: provide a query, e.g. `specky search "refund flow"`', file=sys.stderr)
            sys.exit(1)
        try:
            hits = search_index(repo_root(), args.query)
        except Exception as exc:
            print(f"specky search: {exc}", file=sys.stderr)
            sys.exit(1)
        if not hits:
            print("specky search: no matches")
        for hit in hits:
            print(f"{hit['path']}  —  {hit['title']}")
            if hit["snippet"]:
                print(f"    {hit['snippet']}")
        return

    if args.command == "render-html":
        from specky.db import repo_root
        from specky.html_render import render_site

        try:
            index_path = render_site(repo_root())
        except Exception as exc:
            print(f"specky render-html: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"specky render-html: wrote {index_path}")
        return

    if args.command in ("features", "workflows"):
        from specky import catalog
        from specky.db import repo_root

        docs = (catalog.list_features if args.command == "features" else catalog.list_workflows)(
            repo_root()
        )
        if not docs:
            print(f"specky {args.command}: none yet — run `specky tag` to classify existing docs")
        for doc in docs:
            tags = f" [{', '.join(doc['tags'])}]" if doc["tags"] else ""
            print(f"{doc['path']}  —  {doc['title']}{tags}  ({doc['commits']} commits)")
        return

    if args.command == "commit-info":
        from specky import catalog
        from specky.db import repo_root

        info = catalog.commit_info(repo_root(), args.sha)
        if not info["docs"]:
            print(f"specky commit-info: no feature/workflow doc linked to {args.sha}")
            return
        print(f"tags: {', '.join(info['tags']) or '(none)'}")
        for doc in info["docs"]:
            print(f"{doc['path']}  —  {doc['title']} ({doc['type']})")
        return

    if args.command == "tags":
        from specky import catalog
        from specky.db import repo_root

        by_tag = catalog.list_tags(repo_root())
        if not by_tag:
            print("specky tags: none yet — run `specky tag` to classify existing docs")
        for tag, docs in by_tag.items():
            print(f"{tag} ({len(docs)})")
            for doc in docs:
                print(f"    {doc['path']}  —  {doc['title']}")
        return

    if args.command == "graph":
        from specky import catalog
        from specky.db import repo_root

        graph = catalog.build_graph(repo_root())
        print(f"```mermaid\n{catalog.to_mermaid(graph)}\n```")
        return

    if args.command == "tag":
        from specky.ai_provider import load_provider_from_toml
        from specky.db import repo_root
        from specky.generator import backfill_tags

        root = repo_root()
        try:
            provider = load_provider_from_toml(root / "specky.toml")
            updated = backfill_tags(root, provider)
        except Exception as exc:
            print(f"specky tag: {exc}", file=sys.stderr)
            sys.exit(1)
        if not updated:
            print("specky tag: already up to date")
        else:
            for path in updated:
                print(f"specky tag: tagged {path}")
        return

    if args.command == "serve":
        from specky.chat_server import DEFAULT_PORT, serve as run_serve
        from specky.db import repo_root

        try:
            run_serve(repo_root(), port=args.port or DEFAULT_PORT)
        except OSError as exc:
            print(f"specky serve: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    phase = _NOT_YET_IMPLEMENTED.get(args.command)
    print(f"specky {args.command}: not implemented yet ({phase})", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
