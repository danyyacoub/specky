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

# The one module-level specky import, for `--docs-root`'s help text: `paths` imports nothing from
# specky and nothing outside the stdlib, and `specs` is never spelled out anywhere but there.
from specky.paths import DEFAULT_DOCS_ROOT


def _init(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.setup_wizard import InitOptions, run_init

    options = InitOptions(
        provider=args.provider,
        model=args.model,
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        command=args.provider_command,
        docs_root=args.docs_root,
        assume_yes=args.yes,
        validate=not args.no_validate,
    )
    # Caught here rather than as the `EOFError` `input()` would raise a question or two in: a setup
    # script's log should say which flag it was missing, not name a builtin.
    if not options.non_interactive and not sys.stdin.isatty():
        raise ValueError(
            "stdin is not a terminal, so there's nobody to interview. Pass --yes to take the "
            "defaults, or --provider (with --model/--api-key-env/--base-url/--command) to answer "
            "up front."
        )
    # The repo root, not the cwd: specky.toml is looked for beside `.git` by every reader of it, so
    # `specky init` run from a subdirectory has to write it there too.
    run_init(repo_root() / "specky.toml", options=options)


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


def _export(args: argparse.Namespace) -> None:
    from specky.db import repo_root
    from specky.export import report_lines, run_export

    result = run_export(
        repo_root(),
        mode="confluence" if args.confluence else "single-page",
        include_history=args.include_history,
        pdf=args.pdf,
        title=args.title,
    )
    for line in report_lines(result):
        print(f"specky export: {line}")


def _adopt(args: argparse.Namespace) -> None:
    from specky.adopt import report_lines, run_adopt
    from specky.db import repo_root

    report = run_adopt(
        repo_root(),
        mode=args.mode,
        domain=args.domain,
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        dry_run=args.dry_run,
        assume_yes=args.yes,
    )
    # No `specky adopt:` prefix per line: the report's own first line carries it, and the rest is an
    # indented list plus the next-steps block, which a prefix on every line would make unreadable.
    print("\n".join(report_lines(report)))


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

    commit_doc_main(rewritten=args.rewritten)  # never raises by design — see its docstring


def _install_git_hook(args: argparse.Namespace) -> None:
    from specky.commit_doc import install_git_hook

    for path in install_git_hook():
        print(f"Installed {path}")


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

    init_cmd = command("init", "Choose/configure an AI provider, writes specky.toml", _init)
    # The interview is the default, but every answer it asks for is also a flag, so a machine can
    # set specky up: a Devin blueprint step, a Dockerfile, a CI job. See `InitOptions`.
    init_cmd.add_argument(
        "--yes",
        action="store_true",
        help="Don't ask anything; take the defaults for whatever no flag names",
    )
    init_cmd.add_argument(
        "--provider",
        choices=["anthropic", "openai-compatible", "command"],
        help="Answer the provider question up front (implies --yes for the rest)",
    )
    init_cmd.add_argument("--model", help="Model name, for anthropic and openai-compatible")
    init_cmd.add_argument(
        "--api-key-env",
        metavar="VAR",
        help="Name of the env var holding the API key — never the key itself",
    )
    init_cmd.add_argument("--base-url", help="Endpoint, for --provider openai-compatible")
    init_cmd.add_argument(
        "--command",
        dest="provider_command",
        metavar="CMD",
        help="Command reading the prompt on stdin, for --provider command",
    )
    init_cmd.add_argument(
        "--docs-root",
        metavar="NAME",
        help=f"Generate docs into NAME/ instead of {DEFAULT_DOCS_ROOT}/, writing `[docs] root`",
    )
    init_cmd.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the test call that proves the provider works (no key needed, none spent)",
    )
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
    export_cmd = command(
        "export",
        "Write the docs as one self-contained HTML file, a PDF, or Confluence storage format",
        _export,
    )
    # Not `--single-page`, which would be a flag whose only job is to name the default. The two
    # output shapes are mutually exclusive, so the second one is the flag — and `--pdf` prints the
    # single page, so asking for both shapes at once is a mistake worth failing on rather than
    # taking one and dropping the other in silence.
    shape = export_cmd.add_mutually_exclusive_group()
    shape.add_argument(
        "--confluence",
        action="store_true",
        help="One Confluence storage-format XHTML per doc plus an index, instead of a single page",
    )
    shape.add_argument(
        "--pdf",
        action="store_true",
        help="Also print the single page to PDF with weasyprint, if it's installed",
    )
    export_cmd.add_argument(
        "--include-history",
        action="store_true",
        help="Include specs/history/ — one doc per commit, so this grows with the repo's history",
    )
    export_cmd.add_argument(
        "--title", default="Documentation", help="Title on the cover and in the browser tab"
    )
    adopt_cmd = command(
        "adopt",
        "Import the repo's existing markdown (docs/, adr/, ARCHITECTURE.md) into the docs tree",
        _adopt,
    )
    adopt_cmd.add_argument(
        "--dry-run", action="store_true", help="List what it would import, touch nothing"
    )
    # The three source-file dispositions are one choice, not three flags: `--move --keep` together
    # has no meaning, and a mutually exclusive group says so in `--help` and enforces it for free.
    disposition = adopt_cmd.add_mutually_exclusive_group()
    disposition.add_argument(
        "--move",
        dest="mode",
        action="store_const",
        const="move",
        help="git mv the original into the docs tree (the default)",
    )
    disposition.add_argument(
        "--keep",
        dest="mode",
        action="store_const",
        const="keep",
        help="Copy instead, leaving the original where it is (two live copies to keep in step)",
    )
    disposition.add_argument(
        "--stub",
        dest="mode",
        action="store_const",
        const="stub",
        help="Move, leaving a one-line link behind for anything pointing at the old path",
    )
    adopt_cmd.set_defaults(mode="move")
    adopt_cmd.add_argument(
        "--domain",
        help="Put every adopted doc in this domain, instead of reading one per source path",
    )
    adopt_cmd.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="Also adopt paths matching GLOB, e.g. 'wiki/**/*.md' (repeatable)",
    )
    adopt_cmd.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Never adopt paths matching GLOB, e.g. 'docs/vendor/*' (repeatable, wins over --include)",
    )
    adopt_cmd.add_argument(
        "--yes", action="store_true", help="Skip the confirmation for a large import"
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
    commit_doc_cmd = command(
        "commit-doc",
        "Document the undocumented commits at the tip of this branch (invoked by the git hooks)",
        _commit_doc,
    )
    commit_doc_cmd.add_argument(
        "--rewritten",
        action="store_true",
        help="Read `<old-sha> <new-sha>` pairs on stdin and rename the history docs they "
        "invalidated (the post-rewrite hook's contract — an amend or a rebase)",
    )
    command(
        "install-git-hook",
        "Install the post-commit, post-merge and post-rewrite doc hooks",
        _install_git_hook,
    )
    sync_cmd = command(
        "sync",
        "Backfill micro-docs for any commit that doesn't have one yet (idempotent)",
        _sync,
    )
    sync_cmd.add_argument(
        "--since",
        metavar="REV|DATE",
        help='e.g. v1.2.0, HEAD~50, or "2 weeks ago" (default: newest 10 commits)',
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


def known_flags() -> set[str]:
    """Every long option this CLI actually accepts, asked of the parser instead of listed here.

    `generator.ungrounded_flags` checks a regenerated doc against this, because a doc claiming a
    flag that doesn't exist is the one kind of generation error a machine can settle by itself. Kept
    here rather than there so it can never drift from `build_parser`: adding an argument above
    updates this by construction.
    """
    parser = build_parser()
    subparsers = [
        sub
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for sub in action.choices.values()
    ]
    return {
        option
        for action in [*parser._actions, *(a for sub in subparsers for a in sub._actions)]
        for option in action.option_strings
        if option.startswith("--")
    }


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except Exception as exc:  # provider/network/config/git failures, and handler misuse
        print(f"specky {args.command}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
