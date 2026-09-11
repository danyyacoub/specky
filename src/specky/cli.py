"""`specky` console-script entry point. Subcommands are filled in as their phase lands."""

import argparse
import sys
from pathlib import Path

_NOT_YET_IMPLEMENTED = {
    "generate": "performed by the document-domain skill directly, not scheduled as a CLI command",
    "serve": "Phase 4",
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
    subparsers.add_parser("serve", help="Run the local AI chat companion server (Phase 4)")
    subparsers.add_parser("mcp", help="Run the MCP server over stdio")
    subparsers.add_parser(
        "commit-doc", help="Record a micro-doc for HEAD (invoked by the post-commit git hook)"
    )
    subparsers.add_parser("install-git-hook", help="Install the post-commit micro-doc hook")
    subparsers.add_parser(
        "sync", help="Backfill micro-docs for any commit that doesn't have one yet (idempotent)"
    )

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

    phase = _NOT_YET_IMPLEMENTED.get(args.command)
    print(f"specky {args.command}: not implemented yet ({phase})", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
