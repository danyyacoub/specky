"""`specky init` — choose/configure an AI provider, writes specky.toml.

Interactive by default, because the interview is the friendliest way to hand someone a working
provider config. But the same answers can be supplied up front as an `InitOptions`, and that path
is not a convenience: a setup script has no terminal, and `input()` there raises `EOFError`
*mid-interview* — after some questions have been answered and before anything has been written.
Anywhere specky is installed by a machine rather than a person (a Devin blueprint, a Dockerfile, a
CI job priming a cache) needs the answers to arrive as flags.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from specky.ai_provider import AGENTS, ConfigError, agent_command, current_agent, load_provider
from specky.paths import DEFAULT_DOCS_ROOT

# How many of the files already living in the docs root get named when reporting a collision.
CONFLICT_LIST_LIMIT = 5

# The tables `init` writes, and so rewrites whole on every run. Any other table in an existing
# specky.toml — `[serve]`, `[skills]` — was put there by someone else and is kept as written.
INIT_TABLES = frozenset({"ai", "docs"})

# A table header: `[serve]`, `[ai.retry]`, `[[x]]`, `  [ "docs" ]  # note`.
_TABLE_HEADER = re.compile(r"^\s*\[\[?([^\[\]]+)\]\]?\s*(?:#.*)?$")


@dataclass(frozen=True)
class InitOptions:
    """Answers `run_init` would otherwise have asked for, supplied by the caller.

    `assume_yes` on its own means "the defaults are fine" — the current coding agent on its own
    default model — so `specky init --yes` is the one-liner a setup script wants. Naming a `provider`
    also implies non-interactive: the interview exists to find out which provider, and a caller
    that already said stops having a question to answer.
    """

    provider: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    agent: str | None = None
    docs_root: str | None = None
    assume_yes: bool = False
    # A live provider call is the point of `init` for a human — it's how they find out the key
    # works before the first commit does. A snapshot build that bakes the config in has no key in
    # the environment yet, and shouldn't fail (or bill) for that.
    validate: bool = True

    @property
    def non_interactive(self) -> bool:
        return self.assume_yes or self.provider is not None


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_toml(config: dict, docs_root: str | None = None) -> str:
    """`docs_root` is the *override*, or `None` to record the default — either way the `[docs]`
    table is always written, so specky.toml states plainly which tree it's using rather than
    leaving a reader to know the default by heart."""
    lines = ["[ai]"] + [f'{key} = "{_escape(value)}"' for key, value in config.items()]
    lines += ["", "[docs]", f'root = "{_escape(docs_root or DEFAULT_DOCS_ROOT)}"']
    return "\n".join(lines) + "\n"


def _read_existing(config_path: Path) -> str | None:
    """The current specky.toml's text, or None if there isn't one.

    Read before the interview rather than at the write, so a file init can't parse stops it before
    anyone answers a question or a provider is billed for the test call.
    """
    if not config_path.exists():
        return None
    text = config_path.read_text()
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{config_path.name} isn't valid TOML ({exc}), so init can't tell which of its tables "
            "to keep. Fix it or delete it, then re-run specky init."
        ) from exc
    return text


def _table_of(line: str) -> str | None:
    """The top-level table a header line opens — `ai` for `[ai.retry]` — or None for any other line."""
    match = _TABLE_HEADER.match(line)
    if not match:
        return None
    return match.group(1).split(".")[0].strip().strip("\"'")


def _split_tables(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The keys above the first header, then each table as `(name, text)`, comments included.

    A comment directly above a header — no blank line between — belongs to that header's table, so
    a note on `[serve]` travels with `[serve]` rather than with whatever table came before it.
    """
    root: list[str] = []
    tables: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        current = tables[-1][1] if tables else root
        name = _table_of(line)
        if name is None:
            current.append(line)
            continue
        lead: list[str] = []
        while current and current[-1].lstrip().startswith("#"):
            lead.insert(0, current.pop())
        tables.append((name, lead + [line]))
    return "\n".join(root).strip(), [(name, "\n".join(lines).strip()) for name, lines in tables]


def _merge_toml(
    existing: str | None, config: dict, docs_root: str | None
) -> tuple[str, list[str]]:
    """The new `[ai]` and `[docs]` with every other table of `existing` kept as written, and the
    names of the tables kept.

    The merge is parsed and compared with `existing` before anything is written: a table it would
    lose or change — a `[`-led line inside a multi-line string, read as a header — is a
    `ConfigError`, not a specky.toml quietly missing the viewer's port.
    """
    fresh = _render_toml(config, docs_root)
    if existing is None:
        return fresh, []
    root, tables = _split_tables(existing)
    kept = [(name, body) for name, body in tables if name not in INIT_TABLES]
    merged = "\n\n".join(p for p in (root, fresh.rstrip("\n"), *(b for _, b in kept)) if p) + "\n"

    def others(text: str) -> dict:
        return {k: v for k, v in tomllib.loads(text).items() if k not in INIT_TABLES}

    try:
        intact = others(merged) == others(existing)
    except tomllib.TOMLDecodeError:
        intact = False
    if not intact:
        raise ConfigError(
            "init couldn't rewrite [ai] and [docs] without changing the rest of specky.toml. Move "
            "anything besides those two tables aside, re-run specky init, then add it back."
        )
    return merged, list(dict.fromkeys(name for name, _ in kept))


def foreign_files(repo_root: Path, root: str) -> list[str]:
    """Files in `root` that specky can't have written, newest-shallowest first.

    Any non-markdown file is the tell. specky's tree is `<domain>/<topic>.md` and nothing else, so
    a `specs/openapi.yaml`, a `specs/src/lib.rs` (the Rust ECS crate) or a `specs/*.json` means the
    directory already belongs to something else — and specky generating docs into it would mix two
    unrelated trees together, which no command can unpick afterwards.
    """
    directory = repo_root / root
    if not directory.is_dir():
        return []
    return sorted(
        p.relative_to(repo_root).as_posix()
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() != ".md"
    )


def _validate_docs_root(raw: str) -> str:
    """The docs root as it goes into `[docs] root`, or `ConfigError` if it escapes the repo.

    `raw` rather than the stripped form for the absolute check: stripping the slashes that tidy
    `"documentation/"` would turn `/etc/specs` into the innocuous-looking relative `etc/specs`.
    """
    answer = raw.strip("/")
    if raw.startswith("/") or ".." in Path(answer).parts:
        raise ConfigError(f"Docs root must be a path inside the repo: {raw!r}")
    return answer


def _choose_docs_root(
    repo_root: Path,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> str | None:
    """The docs root to record, or None to accept the default and write no `[docs]` table.

    Only asked when there's a reason to: on a repo where `specs/` is free (the overwhelming
    majority) this is silent, and the question only appears for the repo that would otherwise have
    had specky write into a directory it already uses.
    """
    conflicts = foreign_files(repo_root, DEFAULT_DOCS_ROOT)
    if not conflicts:
        return None

    named = ", ".join(conflicts[:CONFLICT_LIST_LIMIT])
    more = f" and {len(conflicts) - CONFLICT_LIST_LIMIT} more" if len(conflicts) > CONFLICT_LIST_LIMIT else ""
    print_fn(
        f"\n{DEFAULT_DOCS_ROOT}/ already holds files specky didn't write ({named}{more}).\n"
        "specky would generate its docs into that same directory, mixing the two trees."
    )
    raw = input_fn(f"Docs root to use instead [{DEFAULT_DOCS_ROOT}]: ").strip()
    # Rejected rather than silently defaulted, unlike `DocsConfig.load` — there's a human here to
    # retype it.
    answer = _validate_docs_root(raw)
    if not answer or answer == DEFAULT_DOCS_ROOT:
        print_fn(f"Keeping {DEFAULT_DOCS_ROOT}/ — specky's docs will sit alongside what's there.")
        return None
    return answer


def _config_from_options(options: InitOptions) -> dict:
    """The `[ai]` table for a caller that answered up front.

    Every field a provider needs and can't be defaulted is required *here*, before the file is
    written, so a scripted setup fails on the flag it's missing rather than writing a specky.toml
    that only breaks on the first commit.
    """
    kind = options.provider or "agent"
    if kind == "agent":
        agent = options.agent or current_agent()
        if not agent:
            raise ConfigError(
                f"no coding agent found on PATH ({', '.join(AGENTS)}). Install one, or pass "
                "--provider openai-compatible --base-url URL --model NAME --api-key-env VAR"
            )
        agent_command(agent)  # rejects an unknown name before anything is written
        return _agent_config(agent, options.model or "")
    if kind == "openai-compatible":
        required = {
            "--base-url": options.base_url,
            "--model": options.model,
            "--api-key-env": options.api_key_env,
        }
        if missing := [flag for flag, value in required.items() if not value]:
            raise ConfigError(f"provider 'openai-compatible' needs {', '.join(missing)}")
        return {
            "provider": "openai-compatible",
            "base_url": options.base_url,
            "model": options.model,
            "api_key_env": options.api_key_env,
        }
    raise ConfigError(f"Unknown provider: {kind!r} (expected agent or openai-compatible)")


def _docs_root_from_options(
    repo_root: Path, options: InitOptions, print_fn: Callable[[str], None]
) -> str | None:
    if options.docs_root is not None:
        return _validate_docs_root(options.docs_root) or None
    # The collision `_choose_docs_root` would have asked about. Nobody here to ask, and refusing to
    # write a config over it would be worse than the mixed tree — so it's reported and accepted,
    # with the flag that fixes it named.
    if conflicts := foreign_files(repo_root, DEFAULT_DOCS_ROOT):
        named = ", ".join(conflicts[:CONFLICT_LIST_LIMIT])
        print_fn(
            f"{DEFAULT_DOCS_ROOT}/ already holds files specky didn't write ({named}); keeping it. "
            "Pass --docs-root NAME to generate docs somewhere else."
        )
    return None


def run_init(
    config_path: Path,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    options: InitOptions | None = None,
) -> Path:
    """Ask which AI provider to use, validate it with a live call, then write specky.toml.

    Asks nothing when `options.non_interactive` — see `InitOptions`.
    """
    options = options or InitOptions()
    existing = _read_existing(config_path)
    if options.non_interactive:
        config = _config_from_options(options)
        docs_root = _docs_root_from_options(config_path.parent, options, print_fn)
        return _finish_init(config_path, config, docs_root, existing, options.validate, print_fn)

    config = _interview_provider(input_fn, print_fn)

    # Asked before the live call, so the whole interview happens up front rather than either side
    # of a network wait.
    docs_root = _choose_docs_root(config_path.parent, input_fn, print_fn)
    return _finish_init(config_path, config, docs_root, existing, options.validate, print_fn)


def _agent_config(agent: str, model: str) -> dict:
    # No `model` key at all for the agent's own default, rather than an empty one: the agent then
    # keeps following whatever its own settings say.
    return {"provider": "agent", "agent": agent, **({"model": model} if model else {})}


def _interview_provider(input_fn: Callable[[str], str], print_fn: Callable[[str], None]) -> dict:
    """The current coding agent, or an OpenAI-compatible API when there's none or it's declined.

    The agent comes first: it's logged in and paid for already, so it needs no key and no endpoint.
    """
    agent = current_agent()
    if agent:
        label = AGENTS[agent].label
        use = input_fn(f"Use your coding agent, {label} ({agent})? [Y/n] ").strip().lower()
        if use in ("", "y", "yes"):
            model = input_fn(f"Model (blank for {label}'s own default): ").strip()
            return _agent_config(agent, model)
    else:
        print_fn(f"No coding agent found on PATH ({', '.join(AGENTS)}), so specky needs an API.")
    print_fn("OpenAI-compatible API:")
    return {
        "provider": "openai-compatible",
        "base_url": input_fn("Base URL (e.g. https://api.deepseek.com): ").strip(),
        "model": input_fn("Model (e.g. deepseek-chat): ").strip(),
        "api_key_env": input_fn("Env var holding the API key (e.g. DEEPSEEK_API_KEY): ").strip(),
    }


def _ensure_ignored(config_path: Path, print_fn: Callable[[str], None]) -> None:
    """Add specky.toml to the repo's `.gitignore` unless git already ignores it.

    specky.toml is per-machine — which provider, which env var holds the key — and everything
    downstream assumes it stays out of commits: CI writes its own, and the docs root CI must see
    goes in pyproject.toml instead. A repo adopting specky has no rule for it yet, so without this
    the first `git add -A` after `init` commits one developer's provider choice for everyone.

    Left alone: a file that's already tracked (someone decided to commit it, and a `.gitignore`
    entry wouldn't untrack it anyway), and a directory git can't answer for (not a repo, no git).
    """
    repo_root, name = config_path.parent, config_path.name
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", name], cwd=repo_root, capture_output=True
        )
        if tracked.returncode == 0:
            return
        # 0 = ignored already, 1 = not ignored, 128 = not a git repo.
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=repo_root, capture_output=True
        )
    except OSError:
        return
    if ignored.returncode != 1:
        return
    gitignore = repo_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{separator}{name}\n")
    print_fn(f"Added {name} to .gitignore — it's per-machine config, not something to commit")


def _finish_init(
    config_path: Path,
    config: dict,
    docs_root: str | None,
    existing: str | None,
    validate: bool,
    print_fn: Callable[[str], None],
) -> Path:
    """Validate the answers against the real provider, then write them out."""
    # Merged first, so a file init can't rewrite safely fails before the provider call is billed.
    text, kept = _merge_toml(existing, config, docs_root)
    if validate:
        print_fn("Validating provider with a test call...")
        provider = load_provider(config)
        reply = provider.generate("Reply with exactly: ok")
        print_fn(f"Provider responded: {reply.strip()!r}")

    config_path.write_text(text)
    print_fn(f"Wrote {config_path}")
    if kept:
        tables = ", ".join(f"[{name}]" for name in kept)
        print_fn(f"Kept {tables} from the existing {config_path.name}")
    _ensure_ignored(config_path, print_fn)
    if docs_root:
        # specky.toml is gitignored, so CI reads the committed copy or falls back to `specs` — and
        # a `specky check` pointed at the wrong tree finds no docs and reports no coverage.
        print_fn(
            f"\n{config_path.name} is gitignored, so commit the same root for CI to see. Add to "
            f"pyproject.toml:\n\n    [tool.specky.docs]\n    root = \"{docs_root}\"\n"
        )
    return config_path
