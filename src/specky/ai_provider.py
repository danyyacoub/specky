"""AI provider abstraction: anthropic / openai-compatible / command.

Providers are configured in specky.toml under an [ai] table and constructed via
`load_provider`. Each provider exposes a single `generate(prompt) -> str`.

Output length matters here in a way it doesn't for a chat UI: a whole feature doc
(What It Does / How It Works / Outcomes / Acceptance Tests, with tables) is produced in one
call, and a response cut off at the token limit would be written to specs/ as if it were
complete. So every provider sends an explicit `max_tokens` (DEFAULT_MAX_TOKENS, overridable
per-repo via `[ai] max_tokens`), and a truncated response raises `TruncatedResponse` rather
than returning a half-written doc.

`load_provider_from_toml` wraps whichever provider it built in `CachingProvider` unless
`[ai] cache = false`, so an identical prompt is answered from the repo's index instead of paid for
twice, and every call — hit or miss — leaves a row for `specky cost` (see cost.py).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from specky.db import connect

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


# How many characters of cached responses to keep. The cache lives in the gitignored index and its
# only job is to stop a re-run paying twice, so evicting the oldest entries costs at most a repeated
# call — while *not* bounding it would let a backfill over a 10,000-commit history write hundreds
# of megabytes into a file the user never asked to grow. 20 MB is thousands of docs on any repo.
PROMPT_CACHE_MAX_CHARS = 20_000_000


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@dataclass
class CachingProvider:
    """Memoizes an inner provider in the repo's index, and records every call for `specky cost`.

    Same one-method protocol as the providers it wraps, and applied in `load_provider_from_toml`,
    which is the only construction path — so every caller is cached without knowing about it. The
    key is `sha256(model + prompt)`: the model belongs in the key because the same prompt asked of a
    different model is a different answer, and specky's prompts embed the diff, so identical prompts
    only ever come from re-running the same work.

    Both the lookup and the store swallow SQLite errors on purpose. A cache is an optimization, and
    a broken or read-only index must not be the reason a doc doesn't get written.

    A fresh connection per call, rather than one held open: `specky sync` fans its calls out over a
    thread pool, sqlite3 connections aren't shareable across threads, and `connect()` already sets
    WAL and a busy timeout — so this costs a few milliseconds against a call that takes seconds.
    """

    inner: Provider
    repo_root: Path
    model: str
    command: str = ""

    def generate(self, prompt: str) -> str:
        key = hashlib.sha256(f"{self.model}\x00{prompt}".encode()).hexdigest()
        if (hit := self._lookup(key, prompt)) is not None:
            return hit
        response = self.inner.generate(prompt)
        self._store(key, prompt, response)
        return response

    def _lookup(self, key: str, prompt: str) -> str | None:
        try:
            conn = connect(self.repo_root)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "SELECT response FROM prompt_cache WHERE hash = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            self._record(conn, prompt, row[0], cached=True)
            conn.commit()
            return row[0]
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def _store(self, key: str, prompt: str, response: str) -> None:
        try:
            conn = connect(self.repo_root)
        except sqlite3.Error:
            return
        try:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_cache (hash, response, created_at) VALUES (?, ?, ?)",
                (key, response, _now()),
            )
            _prune_cache(conn)
            self._record(conn, prompt, response, cached=False)
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _record(self, conn, prompt: str, response: str, cached: bool) -> None:
        conn.execute(
            "INSERT INTO usage (created_at, command, model, prompt_chars, response_chars, cached) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), self.command, self.model, len(prompt), len(response), int(cached)),
        )


def unwrap(provider: Provider) -> Provider:
    """The concrete provider behind any caching wrapper.

    For code that *inspects* a provider rather than calling it — `specky doctor` reads
    `api_key_env` and `command` off it to report on the environment. Calling code should never use
    this: going around the wrapper is going around the cache and the usage log.
    """
    return getattr(provider, "inner", provider)


def _prune_cache(conn) -> None:
    """Drop the oldest entries until the cache is back inside PROMPT_CACHE_MAX_CHARS.

    Oldest-first rather than least-recently-used: a `specky sync` backfill walks history in order,
    so insertion order is the order the work would be redone in, and the entries most worth keeping
    are the ones written last.
    """
    sql = "SELECT COALESCE(SUM(LENGTH(response)), 0) FROM prompt_cache"
    total = conn.execute(sql).fetchone()[0]
    if total <= PROMPT_CACHE_MAX_CHARS:
        return
    for key, size in conn.execute(
        "SELECT hash, LENGTH(response) FROM prompt_cache ORDER BY created_at"
    ).fetchall():
        conn.execute("DELETE FROM prompt_cache WHERE hash = ?", (key,))
        total -= size
        if total <= PROMPT_CACHE_MAX_CHARS:
            return


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


def _flag(config: dict, key: str, default: bool) -> bool:
    """A boolean from the [ai] table, tolerating a string (the wizard writes every value quoted)."""
    raw = config.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _model_label(config: dict) -> str:
    """What the cache key and `specky cost` call this provider's model.

    A `command` provider has no model name, and the command line is the closest honest stand-in —
    it has to be part of the key, since swapping `claude` for `llm -m gpt-4o` is a different model
    answering the same question.
    """
    if config.get("provider") == "command":
        return f"command:{config.get('command', '')}"
    return str(config.get("model", ""))


def load_provider_from_toml(path: Path, command: str = "") -> Provider:
    """The only construction path, so `[ai] cache` and the usage log apply to every caller.

    `command` is the specky subcommand doing the asking, recorded on each usage row so `specky cost`
    can say which part of specky spent what.
    """
    if not path.exists():
        raise ConfigError(f"{path} not found — run `specky init` first")
    with path.open("rb") as f:
        data = tomllib.load(f)
    ai_config = data.get("ai")
    if not ai_config:
        raise ConfigError(f"{path} has no [ai] table — run `specky init` first")
    provider = load_provider(ai_config)
    if not _flag(ai_config, "cache", True):
        return provider
    return CachingProvider(provider, path.parent, _model_label(ai_config), command)
