"""AI provider abstraction: anthropic / openai-compatible / command.

Providers are configured in specky.toml under an [ai] table and constructed via
`load_provider`. Each provider exposes a single `generate(prompt) -> str`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Provider(Protocol):
    def generate(self, prompt: str) -> str: ...


@dataclass
class AnthropicProvider:
    model: str = "claude-haiku-4-5"
    api_key_env: str = "ANTHROPIC_API_KEY"

    def generate(self, prompt: str) -> str:
        import anthropic

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


@dataclass
class OpenAICompatibleProvider:
    base_url: str
    model: str
    api_key_env: str

    def generate(self, prompt: str) -> str:
        import httpx

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": self.model, "messages": [{"role": "user", "content": prompt}]},
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


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


def load_provider(config: dict) -> Provider:
    """Build a Provider from an already-parsed [ai] table (see load_provider_from_toml)."""
    kind = config.get("provider")
    if kind == "anthropic":
        return AnthropicProvider(
            model=config.get("model", "claude-haiku-4-5"),
            api_key_env=config.get("api_key_env", "ANTHROPIC_API_KEY"),
        )
    if kind == "openai-compatible":
        for field in ("base_url", "model", "api_key_env"):
            if not config.get(field):
                raise ConfigError(f"[ai] provider=\"openai-compatible\" requires '{field}'")
        return OpenAICompatibleProvider(
            base_url=config["base_url"], model=config["model"], api_key_env=config["api_key_env"]
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
