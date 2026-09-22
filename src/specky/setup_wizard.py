"""`specky init` — choose/configure an AI provider, writes specky.toml.

Interactive by default, because the interview is the friendliest way to hand someone a working
provider config. But the same answers can be supplied up front as an `InitOptions`, and that path
is not a convenience: a setup script has no terminal, and `input()` there raises `EOFError`
*mid-interview* — after some questions have been answered and before anything has been written.
Anywhere specky is installed by a machine rather than a person (a Devin blueprint, a Dockerfile, a
CI job priming a cache) needs the answers to arrive as flags.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from specky.ai_provider import ConfigError, load_provider
from specky.paths import DEFAULT_DOCS_ROOT

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"

# How many of the files already living in the docs root get named when reporting a collision.
CONFLICT_LIST_LIMIT = 5


@dataclass(frozen=True)
class InitOptions:
    """Answers `run_init` would otherwise have asked for, supplied by the caller.

    `assume_yes` on its own means "the defaults are fine" — the same choice the first prompt
    offers — so `specky init --yes` is the one-liner a setup script wants. Naming a `provider`
    also implies non-interactive: the interview exists to find out which provider, and a caller
    that already said stops having a question to answer.
    """

    provider: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    command: str | None = None
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
    kind = options.provider or "anthropic"
    if kind == "anthropic":
        return {
            "provider": "anthropic",
            "model": options.model or DEFAULT_MODEL,
            "api_key_env": options.api_key_env or DEFAULT_API_KEY_ENV,
        }
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
    if kind == "command":
        if not options.command:
            raise ConfigError("provider 'command' needs --command")
        return {"provider": "command", "command": options.command}
    raise ConfigError(f"Unknown provider: {kind!r}")


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
    if options.non_interactive:
        config = _config_from_options(options)
        docs_root = _docs_root_from_options(config_path.parent, options, print_fn)
        return _finish_init(config_path, config, docs_root, options.validate, print_fn)

    use_default = input_fn(f"Use the default Anthropic provider ({DEFAULT_MODEL})? [Y/n] ").strip().lower()

    if use_default in ("", "y", "yes"):
        config = {"provider": "anthropic", "model": DEFAULT_MODEL, "api_key_env": DEFAULT_API_KEY_ENV}
    else:
        kind = input_fn("Provider (anthropic/openai-compatible/command): ").strip()
        if kind == "anthropic":
            model = input_fn(f"Model [{DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL
            api_key_env = (
                input_fn(f"Env var holding the API key [{DEFAULT_API_KEY_ENV}]: ").strip()
                or DEFAULT_API_KEY_ENV
            )
            config = {"provider": "anthropic", "model": model, "api_key_env": api_key_env}
        elif kind == "openai-compatible":
            config = {
                "provider": "openai-compatible",
                "base_url": input_fn("Base URL (e.g. https://api.deepseek.com): ").strip(),
                "model": input_fn("Model (e.g. deepseek-chat): ").strip(),
                "api_key_env": input_fn("Env var holding the API key (e.g. DEEPSEEK_API_KEY): ").strip(),
            }
        elif kind == "command":
            config = {
                "provider": "command",
                "command": input_fn(
                    "Command (reads the prompt on stdin, writes the completion to stdout): "
                ).strip(),
            }
        else:
            raise ConfigError(f"Unknown provider: {kind!r}")

    # Asked before the live call, so the whole interview happens up front rather than either side
    # of a network wait.
    docs_root = _choose_docs_root(config_path.parent, input_fn, print_fn)
    return _finish_init(config_path, config, docs_root, options.validate, print_fn)


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
    validate: bool,
    print_fn: Callable[[str], None],
) -> Path:
    """Validate the answers against the real provider, then write them out."""
    if validate:
        print_fn("Validating provider with a test call...")
        provider = load_provider(config)
        reply = provider.generate("Reply with exactly: ok")
        print_fn(f"Provider responded: {reply.strip()!r}")

    config_path.write_text(_render_toml(config, docs_root))
    print_fn(f"Wrote {config_path}")
    _ensure_ignored(config_path, print_fn)
    if docs_root:
        # specky.toml is gitignored, so CI reads the committed copy or falls back to `specs` — and
        # a `specky check` pointed at the wrong tree finds no docs and reports no coverage.
        print_fn(
            f"\n{config_path.name} is gitignored, so commit the same root for CI to see. Add to "
            f"pyproject.toml:\n\n    [tool.specky.docs]\n    root = \"{docs_root}\"\n"
        )
    return config_path
