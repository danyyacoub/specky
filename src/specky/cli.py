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


def _doctor(args: argparse.Namespace) -> None:
    import json

    from specky.doctor import FAIL, report, run_checks, worst

    checks = run_checks()
    print(
        json.dumps([c.as_dict() for c in checks], indent=2)
        if args.json
        else report(checks)
    )
    if worst(checks) == FAIL:
        sys.exit(1)


def _check(args: argparse.Namespace) -> None:
    import json

    from specky.check import report_lines, run_check
    from specky.db import repo_root

    report = run_check(repo_root(), base=args.base, since=args.since)
    print(json.dumps(report.as_dict(), indent=2) if args.json else "\n".join(report_lines(report)))
    if report.violations and not args.advisory:
        sys.exit(1)


def _pr_comment(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.prcomment import comment_markdown, run_pr_comment

    # Bare markdown on stdout, no `specky pr-comment:` prefix and no extra lines: this is meant to
    # be piped straight into `gh pr comment --body-file -`.
    print(
        comment_markdown(run_pr_comment(repo_root(), base=args.base, since=args.since)),
        end="",
    )


def _cost(args: argparse.Namespace) -> None:
    import json

    from specky.cost import clear_cache, report_lines, run_cost
    from specky.db import repo_root

    root = repo_root()
    if args.clear_cache:
        print(f"specky cost: cleared {clear_cache(root)} cached response(s)")
        return
    report = run_cost(root, since=args.since)
    print(json.dumps(report.as_dict(), indent=2) if args.json else "\n".join(report_lines(report)))


def _tests(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.testgen import emit, report_lines

    for line in report_lines(emit(repo_root(), force=args.force)):
        print(f"specky tests: {line}")


def _setup_diagrams(args: argparse.Namespace) -> None:
    from specky.mermaid_tool import setup

    for line in setup(force=args.force):
        print(line)


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
    sync(
        since=args.since,
        limit=args.limit,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        all_branches=args.all_branches,
    )


def _tag(args: argparse.Namespace) -> None:
    from specky.ai_provider import load_provider_from_toml
    from specky.db import repo_root
    from specky.generator import backfill_tags

    root = repo_root()
    updated = backfill_tags(root, load_provider_from_toml(root / "specky.toml", "tag"))
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
        owner = f"  ask: {doc['owner']}" if doc["owner"] else ""
        print(f"{doc['path']}  —  {doc['title']}{tags}  ({doc['commits']} commits){owner}")


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
    command(
        "doctor",
        "Check this repo's specky setup — toolchain, config, hook, index, site",
        _doctor,
    ).add_argument("--json", action="store_true", help="Machine-readable output")
    check_cmd = command(
        "check",
        "Fail if changed code has a doc describing it that this range didn't update",
        _check,
    )
    check_cmd.add_argument(
        "--base",
        metavar="REV",
        help="Compare against REV (default: origin/HEAD if it resolves, else HEAD~1)",
    )
    check_cmd.add_argument("--since", metavar="REV|DATE", help='e.g. v1.2.0 or "2 weeks ago"')
    check_cmd.add_argument(
        "--advisory", action="store_true", help="Print the same report but always exit 0"
    )
    check_cmd.add_argument("--json", action="store_true", help="Machine-readable output")
    pr_cmd = command(
        "pr-comment",
        "Print a markdown summary of a range's doc changes (pipe it to `gh pr comment`)",
        _pr_comment,
    )
    pr_cmd.add_argument(
        "--base",
        metavar="REV",
        help="Compare against REV (default: origin/HEAD if it resolves, else HEAD~1)",
    )
    pr_cmd.add_argument("--since", metavar="REV|DATE", help='e.g. v1.2.0 or "2 weeks ago"')
    cost_cmd = command(
        "cost", "Report provider calls, cache hit rate and character totals", _cost
    )
    cost_cmd.add_argument(
        "--since", metavar="DATE", help="Only calls at or after DATE (e.g. 2026-09-01)"
    )
    cost_cmd.add_argument(
        "--clear-cache", action="store_true", help="Drop every memoized response, keep the log"
    )
    cost_cmd.add_argument("--json", action="store_true", help="Machine-readable output")
    tests_cmd = command(
        "tests",
        "Write pytest scaffolds from the Given/When/Then tables in specs/",
        _tests,
    )
    tests_cmd.add_argument(
        "--emit",
        default="pytest",
        choices=["pytest"],
        help="Output format (only pytest so far, and it's the default)",
    )
    tests_cmd.add_argument(
        "--force", action="store_true", help="Overwrite scaffolds that already exist"
    )
    command("index", "Index specs/ and git log into SQLite", _index)
    command("search", "Keyword search over the index", _search).add_argument("query", nargs="?")
    command("render-html", "Render the static HTML doc site", _render_html)
    command(
        "setup-diagrams",
        "Install the Node tool that renders ```mermaid``` diagrams (one-time, needs Node)",
        _setup_diagrams,
    ).add_argument(
        "--force", action="store_true", help="Re-run npm install even if it looks up to date"
    )
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
    sync_cmd.add_argument(
        "--all-branches",
        action="store_true",
        help="Document commits on every ref, not just HEAD (more commits, so more AI calls)",
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
