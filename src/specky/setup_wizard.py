"""Interactive `specky init` — choose/configure an AI provider, writes specky.toml."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from specky.ai_provider import ConfigError, load_provider

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_toml(config: dict) -> str:
    lines = ["[ai]"] + [f'{key} = "{_escape(value)}"' for key, value in config.items()]
    return "\n".join(lines) + "\n"


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

    print_fn("Validating provider with a test call...")
    provider = load_provider(config)
    reply = provider.generate("Reply with exactly: ok")
    print_fn(f"Provider responded: {reply.strip()!r}")

    config_path.write_text(_render_toml(config))
    print_fn(f"Wrote {config_path}")
    return config_path
