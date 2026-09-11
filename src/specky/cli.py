"""`specky` console-script entry point. Subcommands are filled in as their phase lands."""

import argparse
import sys

_NOT_YET_IMPLEMENTED = {
    "init": "Phase 2",
    "index": "Phase 3",
    "generate": "Phase 2",
    "render-html": "Phase 3",
    "serve": "Phase 4",
    "install-git-hook": "Phase 2",
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="specky")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Choose/configure an AI provider (Phase 2)")
    subparsers.add_parser("index", help="Index specs/ and git log into SQLite (Phase 3)")
    generate = subparsers.add_parser("generate", help="Generate/update a domain doc (Phase 2)")
    generate.add_argument("--domain", required=False)
    search = subparsers.add_parser("search", help="Keyword search over the index (Phase 3)")
    search.add_argument("query", nargs="?")
    subparsers.add_parser("render-html", help="Render the static HTML doc site (Phase 3)")
    subparsers.add_parser("serve", help="Run the local AI chat companion server (Phase 4)")
    subparsers.add_parser("mcp", help="Run the MCP server over stdio")
    subparsers.add_parser("install-git-hook", help="Install the post-commit micro-doc hook (Phase 2)")

    args = parser.parse_args()

    if args.command == "mcp":
        from specky.mcp_server import main as mcp_main

        mcp_main()
        return

    phase = _NOT_YET_IMPLEMENTED.get(args.command)
    print(f"specky {args.command}: not implemented yet ({phase})", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
