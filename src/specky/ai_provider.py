"""AI provider abstraction: anthropic / openai-compatible / command.

Providers are configured in specky.toml under an [ai] table and constructed via
`load_provider`. Each provider exposes a single `generate(prompt) -> str`.

Output length matters here in a way it doesn't for a chat UI: a whole feature doc
(What It Does / How It Works / Outcomes / Acceptance Tests, with tables) is produced in one
call, and a response cut off at the token limit would be written to specs/ as if it were
complete. So every provider sends an explicit `max_tokens` (DEFAULT_MAX_TOKENS, overridable
per-repo via `[ai] max_tokens`), and a truncated response raises `TruncatedResponse` rather
than returning a half-written doc.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Enough for a full feature doc with tables; a doc that hits even this is a signal the
# prompt or the change is too big, not something to silently accept half of.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0


class Provider(Protocol):
    def generate(self, prompt: str) -> str: ...


class TruncatedResponse(RuntimeError):
    """The model stopped because it hit the output token limit, so the text is incomplete."""


@dataclass
class AnthropicProvider:
    model: str = "claude-haiku-4-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = DEFAULT_MAX_TOKENS

    def generate(self, prompt: str) -> str:
        import anthropic

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        client = anthropic.Anthropic(api_key=api_key, timeout=DEFAULT_TIMEOUT_SECONDS)
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "max_tokens":
            raise TruncatedResponse(
                f"{self.model} hit the {self.max_tokens}-token output limit — "
                "raise `max_tokens` in specky.toml's [ai] table"
            )
        return response.content[0].text


@dataclass
class OpenAICompatibleProvider:
    base_url: str
    model: str
    api_key_env: str
    max_tokens: int = DEFAULT_MAX_TOKENS

    def generate(self, prompt: str) -> str:
        import httpx

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise TruncatedResponse(
                f"{self.model} hit the {self.max_tokens}-token output limit — "
                "raise `max_tokens` in specky.toml's [ai] table"
            )
        return choice["message"]["content"]


@dataclass
class CommandProvider:
    command: str

    def generate(self, prompt: str) -> str:
        result = subprocess.run(
            shlex.split(self.command),
            input=prompt,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()


class ConfigError(RuntimeError):
    pass


def _max_tokens(config: dict) -> int:
    """`[ai] max_tokens`, tolerating a string (setup_wizard writes every value quoted)."""
    raw = config.get("max_tokens", DEFAULT_MAX_TOKENS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"[ai] max_tokens must be an integer, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"[ai] max_tokens must be positive, got {value}")
    return value


def load_provider(config: dict) -> Provider:
    """Build a Provider from an already-parsed [ai] table (see load_provider_from_toml)."""
    kind = config.get("provider")
    if kind == "anthropic":
        return AnthropicProvider(
            model=config.get("model", "claude-haiku-4-5"),
            api_key_env=config.get("api_key_env", "ANTHROPIC_API_KEY"),
            max_tokens=_max_tokens(config),
        )
    if kind == "openai-compatible":
        for field in ("base_url", "model", "api_key_env"):
            if not config.get(field):
                raise ConfigError(f"[ai] provider=\"openai-compatible\" requires '{field}'")
        return OpenAICompatibleProvider(
            base_url=config["base_url"],
            model=config["model"],
            api_key_env=config["api_key_env"],
            max_tokens=_max_tokens(config),
        )
    if kind == "command":
        if not config.get("command"):
            raise ConfigError('[ai] provider="command" requires \'command\'')
        return CommandProvider(command=config["command"])
    raise ConfigError(f"Unknown [ai] provider: {kind!r} (expected anthropic/openai-compatible/command)")


def load_provider_from_toml(path: Path) -> Provider:
    if not path.exists():
        raise ConfigError(f"{path} not found — run `specky init` first")
    with path.open("rb") as f:
        data = tomllib.load(f)
    ai_config = data.get("ai")
    if not ai_config:
        raise ConfigError(f"{path} has no [ai] table — run `specky init` first")
    return load_provider(ai_config)
