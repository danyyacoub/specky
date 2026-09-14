"""Interactive `specky init` — choose/configure an AI provider, writes specky.toml."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from specky.ai_provider import ConfigError, load_provider
from specky.paths import DEFAULT_DOCS_ROOT

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"

# How many of the files already living in the docs root get named when reporting a collision.
CONFLICT_LIST_LIMIT = 5


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_toml(config: dict, docs_root: str | None = None) -> str:
    lines = ["[ai]"] + [f'{key} = "{_escape(value)}"' for key, value in config.items()]
    if docs_root:
        lines += ["", "[docs]", f'root = "{_escape(docs_root)}"']
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
    answer = raw.strip("/")
    if not answer or answer == DEFAULT_DOCS_ROOT:
        print_fn(f"Keeping {DEFAULT_DOCS_ROOT}/ — specky's docs will sit alongside what's there.")
        return None
    # `raw` for the absolute check: stripping the slashes that tidy `"documentation/"` would turn
    # `/etc/specs` into the innocuous-looking relative `etc/specs`. Rejected rather than silently
    # defaulted, unlike `DocsConfig.load` — there's a human here to retype it.
    if raw.startswith("/") or ".." in Path(answer).parts:
        raise ConfigError(f"Docs root must be a path inside the repo: {raw!r}")
    return answer


def run_init(
    config_path: Path,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> Path:
    """Ask which AI provider to use, validate it with a live call, then write specky.toml."""
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

    print_fn("Validating provider with a test call...")
    provider = load_provider(config)
    reply = provider.generate("Reply with exactly: ok")
    print_fn(f"Provider responded: {reply.strip()!r}")

    config_path.write_text(_render_toml(config, docs_root))
    print_fn(f"Wrote {config_path}")
    if docs_root:
        # specky.toml is gitignored, so CI reads the committed copy or falls back to `specs` — and
        # a `specky check` pointed at the wrong tree finds no docs and reports no coverage.
        print_fn(
            f"\n{config_path.name} is gitignored, so commit the same root for CI to see. Add to "
            f"pyproject.toml:\n\n    [tool.specky.docs]\n    root = \"{docs_root}\"\n"
        )
    return config_path
