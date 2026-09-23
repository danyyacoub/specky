"""`specky doctor` — one command that answers "why didn't a doc get generated?".

Everything here was previously only in `scripts/doctor.sh`, which cd's into a specky *checkout*, so
an installed user had no way to run any of it. That's backwards: the person who needs these checks
is the one who ran `uv tool install specky` and got silence.

Two rules shape the implementation:

- **Nothing here costs money or scales with history.** No provider is ever called (the config check
  builds a provider object and stops), and the commit backlog is probed over the last
  `BACKLOG_PROBE_COMMITS` commits rather than walking every commit in the repo — `specky doctor` on
  a repo with 200k commits must still answer in well under a second.
- **A secret is never printed.** The API-key check reports whether the variable named in
  `specky.toml` is set, never any part of its value.

`fail` is reserved for "specky cannot work here, and won't fix itself": no git, unparseable config,
a foreign post-commit hook that `install-git-hook` refuses to overwrite, one of specky's own
dependencies missing from the environment its entry point runs in. Everything a plain command would
fix — no config, no index, no rendered site — is a `warn`, so `specky doctor` exits 0 on a fresh
repo and can be dropped into CI as-is.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from specky import mermaid_tool, paths
from specky.commit_doc import (
    DISABLE_HOOK_ENV,
    HOOK_MARKER,
    HOOKS,
    _AUTO_COMMIT_MARKER,
    history_doc_for,
    hook_disabled,
    hooks_dir,
)
from specky.generator import PENDING_DIR

OK, WARN, FAIL = "ok", "warn", "fail"

# How far back the "did the hook actually run?" probe looks. Bounded on purpose: the answer a user
# needs is "is it working now", and walking all of history to count an old backlog is `specky sync
# --dry-run`'s job, which is where they get sent.
BACKLOG_PROBE_COMMITS = 20


@dataclass(frozen=True)
class Check:
    section: str
    status: str
    detail: str

    def as_dict(self) -> dict:
        return {"section": self.section, "status": self.status, "detail": self.detail}


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _version(executable: str, *flags: str) -> str | None:
    """`<exe> --version`, or None if it isn't on PATH at all."""
    if shutil.which(executable) is None:
        return None
    proc = _run([executable, *(flags or ("--version",))])
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else executable


def _toolchain() -> list[Check]:
    checks = [Check("toolchain", OK, f"python {sys.version.split()[0]} ({sys.executable})")]

    git = _version("git")
    checks.append(
        Check("toolchain", OK, git) if git else Check("toolchain", FAIL, "git not found on PATH")
    )

    uv = _version("uv")
    checks.append(
        Check("toolchain", OK, uv)
        if uv
        else Check("toolchain", WARN, "uv not found — only needed to run specky from a checkout")
    )

    node = _version("node")
    checks.append(
        Check("toolchain", OK, f"node {node}")
        if node
        else Check("toolchain", WARN, "node not found — only needed to render ```mermaid``` diagrams")
    )
    return checks


# specky's own runtime dependencies, each with what stops working when it's absent. Probed by
# import name rather than distribution name, because an install can record a dependency it can't
# import; `test_every_declared_dependency_is_probed` keeps the list level with pyproject.toml's.
RUNTIME_IMPORTS: tuple[tuple[str, str], ...] = (
    ("markdown", "`render-html`, `export` and `serve`"),
    ("jinja2", "`render-html`, `export` and `serve`"),
    ("mcp", "the `specky-mcp` server"),
    ("anthropic", "the `anthropic` provider"),
    ("httpx", "the `openai-compatible` provider"),
)


def _reinstall_hint() -> str:
    """The repair command for *this* install, complete enough to paste.

    An editable install runs straight out of a checkout, so the path is right here in `__file__` and
    worth printing — the person reading this is being told to re-run an install whose path they may
    not remember typing.
    """
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").exists():
        return f"uv tool install --editable {checkout} --force"
    return "uv tool install specky --force"


def _imports() -> list[Check]:
    """Whether specky's own dependencies import in the interpreter its commands will run under.

    Not paranoia about a bad wheel: `uv tool install --editable` resolves dependencies *once*, so a
    dependency added to pyproject.toml afterwards is simply absent from the installed tool while the
    code that imports it ships from the checkout on every run. That is how `markdown` went missing
    for a whole afternoon while `specky doctor` reported the toolchain healthy and `render-html`
    died on `No module named 'markdown'`.

    A `fail`, not a `warn`: no specky command repairs this, only re-installing does.
    """
    missing: list[tuple[str, str]] = []
    for module, breaks in RUNTIME_IMPORTS:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):  # a parent that isn't importable either, or a stale entry
            found = False
        if not found:
            missing.append((module, breaks))

    if not missing:
        return [Check("deps", OK, f"all {len(RUNTIME_IMPORTS)} runtime dependencies importable")]
    return [
        Check(
            "deps",
            FAIL,
            f"{module} is not importable, so {breaks} cannot run — repair this install with "
            f"`{_reinstall_hint()}`",
        )
        for module, breaks in missing
    ]


def _diagrams() -> list[Check]:
    installed = mermaid_tool.tool_dir()
    if installed:
        return [Check("diagrams", OK, f"mermaid renderer installed at {installed}")]
    looked = ", ".join(str(p) for p in mermaid_tool.candidate_dirs())
    return [
        Check(
            "diagrams",
            WARN,
            "mermaid renderer not installed, so diagrams stay as plain text — run "
            f"`specky setup-diagrams` (looked in: {looked})",
        )
    ]


def _config(repo_root: Path) -> list[Check]:
    from specky.ai_provider import (
        AI_ENV_PREFIX,
        ConfigError,
        ai_env_overrides,
        load_provider_from_toml,
        read_ai_config,
        unwrap,
    )

    path = repo_root / "specky.toml"
    overrides = ai_env_overrides()
    from_env = "provider" in overrides
    if not path.exists() and not from_env:
        return [
            Check(
                "config",
                WARN,
                f"{path.name} missing — run `specky init` (or `specky init --provider agent` to "
                "use your coding agent without the interview)",
            )
        ]

    try:
        provider_name = read_ai_config(path).get("provider", "(unset)")
    except tomllib.TOMLDecodeError as exc:
        return [Check("config", FAIL, f"{path.name} is not valid TOML ({exc})")]
    except ConfigError as exc:
        return [Check("config", FAIL, str(exc))]

    try:
        # Unwrapped: this reads fields off the concrete provider, and the caching wrapper doesn't
        # have them.
        provider = unwrap(load_provider_from_toml(path))
    except ConfigError as exc:
        return [Check("config", FAIL, str(exc))]

    source = f"{AI_ENV_PREFIX}PROVIDER" if from_env else path.name
    checks = [Check("config", OK, f'{source}: [ai] provider = "{provider_name}"')]
    # Names only: a deployed server's config lives in its environment, and without this line the
    # report would read as if specky.toml were what's in effect.
    if overrides:
        names = ", ".join(f"{AI_ENV_PREFIX}{key.upper()}" for key in sorted(overrides))
        replaced = " (replacing specky.toml's [ai])" if from_env and path.exists() else ""
        checks.append(Check("config", OK, f"[ai] set from the environment: {names}{replaced}"))

    # Whether the credential is *present*, never what it is.
    key_env = getattr(provider, "api_key_env", None)
    if key_env:
        checks.append(
            Check("config", OK, f"{key_env} is set")
            if os.environ.get(key_env)
            else Check("config", FAIL, f"{key_env} is not set in this environment")
        )

    if getattr(provider, "aws_region", None) is not None:
        checks += _bedrock_checks(provider)

    command = getattr(provider, "command", None)
    if command:
        executable = shlex.split(command)[0] if shlex.split(command) else ""
        checks.append(
            Check("config", OK, f"provider command `{executable}` is on PATH")
            if executable and shutil.which(executable)
            else Check("config", FAIL, f"provider command `{executable}` not found on PATH")
        )

    # Per-task model routing, reported because it is invisible otherwise: the whole point of
    # `[ai] document_model` is to be set once and forgotten, and the only other way to find out
    # which model answered what is to read `specky cost` after the fact.
    checks += _task_model_checks(path)
    return checks


def _bedrock_checks(provider) -> list[Check]:
    """What a Bedrock call needs that specky.toml can't show: the optional AWS SDK, and a region.

    Credentials aren't probed — resolving them means asking AWS, and doctor makes no calls.
    """
    from specky.ai_provider import BEDROCK_INSTALL_HINT

    checks = [
        Check("config", OK, "AWS SDK (botocore) importable for the bedrock provider")
        if importlib.util.find_spec("botocore")
        else Check(
            "config",
            FAIL,
            f"the bedrock provider needs the AWS SDK — run `{BEDROCK_INSTALL_HINT}`",
        )
    ]
    region = (
        provider.aws_region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    checks.append(
        Check("config", OK, f"AWS region: {region}")
        if region
        # A warning: an AWS profile can carry its own region, and doctor doesn't read AWS config.
        else Check(
            "config",
            WARN,
            "no AWS region in `[ai] aws_region` or AWS_REGION — Bedrock calls fail unless your AWS "
            "profile supplies one",
        )
    )
    return checks


def _task_model_checks(path: Path) -> list[Check]:
    from specky.ai_provider import ConfigError, read_ai_config, task_models

    try:
        config = read_ai_config(path)
        routed = task_models(config)
    except ConfigError as exc:
        return [Check("config", FAIL, str(exc))]
    if not routed:
        return []
    default = config.get("model") or "(provider default)"
    listed = ", ".join(f"{task} -> {model}" for task, model in sorted(routed.items()))
    return [Check("config", OK, f"per-task models: {listed} (everything else -> {default})")]


def _history(repo_root: Path) -> list[Check]:
    """Whether git history is all here, because most of specky assumes it is.

    A shallow clone is the one repo state that makes every other check lie: `pending_commits()`
    walks `git log`, so the commits cut off by the clone depth simply aren't there — the backlog
    probe below reports a clean bill of health, `specky sync` finds nothing to backfill, and
    `specky index` records a fraction of the history. CI runners default to shallow (that's why the
    workflows here set `fetch-depth: 0`) and so do some cloud coding-agent VMs, which is exactly
    where nobody is watching the output.

    Silent on a normal clone: a `[ok] not shallow` row would be noise on every machine a developer
    ever runs this on.
    """
    if _run(["git", "rev-parse", "--is-shallow-repository"], cwd=repo_root).stdout.strip() != "true":
        return []
    return [
        Check(
            "repo",
            WARN,
            "shallow clone — the commits below the clone depth are invisible to `specky sync`, "
            "`specky index` and the backlog check below. Run `git fetch --unshallow`",
        )
    ]


def _git_hook(repo_root: Path) -> list[Check]:
    """All three hooks, in the directory git actually runs them from.

    Reported per hook rather than as one verdict, because the interesting case is the *partial*
    install: a repo set up before `post-merge`/`post-rewrite` existed has a working post-commit
    hook and still misses every merge and every rebase. That's a `warn` naming the fix, while a
    hook file specky didn't write stays a `fail` — `install-git-hook` won't overwrite one.
    """
    hooks_path = hooks_dir(repo_root)
    checks = []
    missing = []
    if hook_disabled():
        # First, because it makes every row under it moot: the hooks can all be installed and
        # correct and still document nothing.
        checks.append(
            Check(
                "git hook",
                WARN,
                f"{DISABLE_HOOK_ENV} is set in this environment, so every hook fire returns "
                "without documenting anything",
            )
        )
    for name in HOOKS:
        hook = hooks_path / name
        if not hook.is_file():
            missing.append(name)
        elif HOOK_MARKER not in hook.read_text():
            checks.append(
                Check(
                    "git hook",
                    FAIL,
                    f"{hook} exists but wasn't installed by specky, and `install-git-hook` won't "
                    "overwrite it — add `specky commit-doc || true` to it by hand",
                )
            )
        elif not os.access(hook, os.X_OK):
            checks.append(
                Check("git hook", FAIL, f"{hook} is not executable, so git silently never runs it")
            )
        else:
            checks.append(Check("git hook", OK, f"{name} hook installed at {hook}"))

    if missing:
        installed = len(HOOKS) - len(missing)
        checks.append(
            Check(
                "git hook",
                WARN,
                f"{', '.join(missing)} hook(s) not installed — run `specky install-git-hook`"
                + (
                    ", so commits that arrive by merge, pull, rebase or amend go undocumented"
                    if installed
                    else ""
                ),
            )
        )
    return checks


def _index(repo_root: Path) -> list[Check]:
    path = repo_root / ".specky" / "index.db"
    if not path.exists():
        return [Check("index", WARN, "no index yet — run `specky index`")]
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - needs a corrupt file
        return [Check("index", FAIL, f"{path} can't be opened ({exc})")]
    try:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        commits = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error as exc:
        return [Check("index", FAIL, f"{path} is missing its tables ({exc}) — run `specky index`")]
    finally:
        conn.close()

    checks = [Check("index", OK, f"{docs} docs, {commits} commits indexed ({path})")]
    if journal != "wal":
        # Not fatal, but it's why a hook firing during `specky serve` may hit a locked database.
        checks.append(
            Check("index", WARN, f"journal_mode is {journal}, not wal — re-run `specky index`")
        )
    return checks


def _site(repo_root: Path) -> list[Check]:
    site = repo_root / ".specky" / "site"
    if not (site / "index.html").exists():
        return [Check("site", WARN, "no rendered site yet — run `specky render-html`")]
    pages = sum(1 for _ in site.glob("*.html"))
    return [Check("site", OK, f"{pages} pages at {site / 'index.html'}")]


def _pending(repo_root: Path) -> list[Check]:
    """Doc updates the hook generated but refused to write (see generator.sync_feature_doc).

    A `warn`, not a `fail`: nothing is broken and nothing was lost — a draft is sitting there
    because writing it would have destroyed hand-written content or because it named a flag this
    CLI doesn't have. It stays a warning until someone reads it, which is the whole point of
    surfacing it here rather than in the hook output nobody scrolls back to.
    """
    pending_dir = repo_root / PENDING_DIR
    drafts = sorted(p.relative_to(pending_dir).as_posix() for p in pending_dir.rglob("*.md"))
    if not drafts:
        return [Check("pending", OK, "no refused doc updates waiting")]
    named = ", ".join(drafts[:3]) + (f" and {len(drafts) - 3} more" if len(drafts) > 3 else "")
    return [
        Check(
            "pending",
            WARN,
            f"{len(drafts)} refused doc update(s) waiting in {PENDING_DIR}/ ({named}) — read the "
            "draft against the doc it would have replaced, then keep it or delete it",
        )
    ]


def _backlog(repo_root: Path) -> list[Check]:
    """Whether the hook is producing docs *now*, over a fixed window of recent commits."""
    history_dir = paths.history_dir(repo_root)
    log = _run(
        # --no-merges: merges are never documented, by design (see `pending_commits`).
        ["git", "log", f"-{BACKLOG_PROBE_COMMITS}", "--no-merges", "--format=%H%x1f%s"],
        cwd=repo_root,
    ).stdout
    considered = 0
    missing = 0
    for line in log.splitlines():
        sha, _, subject = line.partition("\x1f")
        if subject.startswith(_AUTO_COMMIT_MARKER):
            continue  # specky's own doc-sync commits are never documented, by design
        considered += 1
        if history_doc_for(history_dir, sha) is None:
            missing += 1

    if not considered:
        return [Check("docs", WARN, "no commits to document yet")]
    if not missing:
        return [Check("docs", OK, f"the last {considered} documentable commits all have a history doc")]
    return [
        Check(
            "docs",
            WARN,
            f"{missing} of the last {considered} documentable commits have no history doc — run "
            "`specky sync --dry-run` to see the full backlog",
        )
    ]


def run_checks() -> list[Check]:
    """Every check, in report order. Never raises: a failed check is a `fail` row, not a traceback,
    because this is the command someone runs *when* things are already broken."""
    checks = _toolchain() + _imports() + _diagrams()

    root = _run(["git", "rev-parse", "--show-toplevel"])
    if root.returncode != 0:
        return [*checks, Check("repo", FAIL, "not inside a git repository — specky is git-shaped")]
    repo_root = Path(root.stdout.strip())
    checks.append(Check("repo", OK, str(repo_root)))

    # `_history` first so its row lands under the `== repo ==` header the line above just opened.
    for section in (_history, _config, _git_hook, _index, _site, _pending, _backlog):
        try:
            checks += section(repo_root)
        except Exception as exc:  # a broken check must not hide the other checks' answers
            checks.append(Check(section.__name__.strip("_"), FAIL, f"check itself failed: {exc}"))
    return checks


def report(checks: list[Check]) -> str:
    """The `[ok]/[warn]/[fail]` lines, grouped by section — same shape scripts/doctor.sh printed."""
    lines: list[str] = []
    section = None
    for check in checks:
        if check.section != section:
            section = check.section
            lines.append(f"== {section} ==")
        lines.append(f"  [{check.status}]".ljust(9) + check.detail)
    return "\n".join(lines)


def worst(checks: list[Check]) -> str:
    for status in (FAIL, WARN):
        if any(c.status == status for c in checks):
            return status
    return OK
