"""AI provider abstraction: anthropic / openai-compatible / command.

Providers are configured in specky.toml under an [ai] table and constructed via
`load_provider`. Each provider exposes `generate(prompt, *, prefix, task) -> str`, where
`prefix` is the stable half of the prompt (cached where the provider supports it) and `task`
names the kind of call, which `[ai] <task>_model` can route to its own model.

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
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from specky.db import connect

# Enough for a full feature doc with tables; a doc that hits even this is a signal the
# prompt or the change is too big, not something to silently accept half of.
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0

# How long a `--batch` run waits, and how often it asks. The API allows a batch 24 hours; specky
# does not, because a command someone is watching must eventually stop. Giving up doesn't cancel
# anything — the batch keeps running server-side and a re-run collects whatever finished, since
# `CachingProvider` answers the parts that already landed.
BATCH_TIMEOUT_SECONDS = 3600.0
BATCH_POLL_SECONDS = 10.0

# The tasks specky asks a model to do, each able to name its own model in `[ai]`. Kept as a tuple
# rather than free-form strings so `specky doctor` can report the routing and a typo in
# `[ai] doc_modle` is a config error rather than a setting that silently does nothing.
TASKS = ("summary", "classify", "doc", "discovery", "glossary", "tag", "chat")


class Provider(Protocol):
    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str: ...


class TruncatedResponse(RuntimeError):
    """The model stopped because it hit the output token limit, so the text is incomplete."""


# What `prefix` is for, since it's the one part of the protocol that isn't obvious:
#
# Specky asks the same model the same *kind* of question hundreds of times in a row, and most of
# each prompt is identical every time — the classification instructions and the list of every doc
# that already exists are resent for every commit, and on a repo with 300 docs that list is the
# single largest thing specky pays for. So a call is split in two: `prefix` is the stable leading
# context, `prompt` is the part that changes. Providers that can cache a prefix do (Anthropic bills
# a cache read at a tenth of the input rate); the rest just concatenate and behave exactly as
# before. Callers that have nothing stable to declare pass `prompt` alone.
#
# The split is by *position*, not by importance: everything in `prefix` is rendered ahead of
# everything in `prompt`, because a prefix cache is a literal prefix match and any byte that varies
# must come after every byte that doesn't.


@dataclass
class AnthropicProvider:
    model: str = "claude-haiku-4-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = DEFAULT_MAX_TOKENS

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        import anthropic

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        client = anthropic.Anthropic(api_key=api_key, timeout=DEFAULT_TIMEOUT_SECONDS)
        # The stable half goes in `system` with a cache breakpoint on it. A prefix shorter than the
        # model's minimum cacheable length simply isn't cached — it is not an error and costs no
        # premium — so this is safe to send whatever the prefix's size, and a small repo pays
        # exactly what it did before.
        #
        # `system` is omitted entirely rather than passed as the SDK's NOT_GIVEN sentinel: the
        # request is identical either way, and not naming the sentinel keeps this from depending on
        # a private-ish corner of the SDK's surface.
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if prefix:
            kwargs["system"] = [
                {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
            ]
        response = client.messages.create(**kwargs)
        if response.stop_reason == "max_tokens":
            raise TruncatedResponse(
                f"{self.model} hit the {self.max_tokens}-token output limit — "
                "raise `max_tokens` in specky.toml's [ai] table"
            )
        return response.content[0].text

    def generate_batch(
        self, prompts: dict[str, tuple[str, str]], task: str = ""
    ) -> dict[str, str]:
        """Answer many independent prompts through the Message Batches API, at half the price.

        `prompts` maps a caller's own key to `(prefix, prompt)`; the result maps the same keys to
        answers. A key whose request failed is absent rather than raising — the caller decides
        whether one bad commit should stop a backfill, and every caller here decides it shouldn't.

        Worth it only for work nobody is waiting on: a batch is asynchronous and the API allows up
        to 24 hours, so this is reached for by `specky sync --batch` and `specky bootstrap --batch`
        and never by a git hook, which must not turn `git commit` into a long poll.
        """
        import anthropic

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        client = anthropic.Anthropic(api_key=api_key, timeout=DEFAULT_TIMEOUT_SECONDS)

        requests = []
        for key, (prefix, prompt) in prompts.items():
            params: dict = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if prefix:
                params["system"] = [
                    {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
                ]
            requests.append({"custom_id": _batch_id(key), "params": params})

        batch = client.messages.batches.create(requests=requests)
        deadline = time.monotonic() + BATCH_TIMEOUT_SECONDS
        while client.messages.batches.retrieve(batch.id).processing_status != "ended":
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"batch {batch.id} was still running after "
                    f"{BATCH_TIMEOUT_SECONDS / 60:.0f} minutes — it keeps going server-side, and "
                    "re-running picks up whatever landed"
                )
            time.sleep(BATCH_POLL_SECONDS)

        # Results come back in any order, so they're keyed by custom_id and never by position.
        by_id = {_batch_id(key): key for key in prompts}
        answers: dict[str, str] = {}
        for result in client.messages.batches.results(batch.id):
            key = by_id.get(result.custom_id)
            if key is None or result.result.type != "succeeded":
                continue
            message = result.result.message
            if message.stop_reason == "max_tokens":
                continue  # same contract as `generate`: a truncated answer is not an answer
            answers[key] = message.content[0].text
        return answers


@dataclass
class OpenAICompatibleProvider:
    base_url: str
    model: str
    api_key_env: str
    max_tokens: int = DEFAULT_MAX_TOKENS

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        import httpx

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        # A system message rather than a concatenation: several OpenAI-compatible endpoints cache
        # or reuse a stable system prefix of their own accord, and none of them are worse off for
        # the split. No `cache_control` — that is Anthropic's spelling, and sending it here would
        # be rejected by some endpoints and ignored by the rest.
        messages = [{"role": "user", "content": prompt}]
        if prefix:
            messages.insert(0, {"role": "system", "content": prefix})
        response = httpx.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
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

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        # An arbitrary CLI has one input channel, so the split is undone here: the command sees
        # exactly the prompt it would have seen before `prefix` existed. Whether anything is cached
        # is then the command's own business — `claude -p` does its own prompt caching, and specky
        # can neither help nor measure it.
        result = subprocess.run(
            shlex.split(self.command),
            input=f"{prefix}\n\n{prompt}" if prefix else prompt,
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
    key is `sha256(model + prefix + prompt)`: the model belongs in the key because the same prompt
    asked of a different model is a different answer, and specky's prompts embed the diff, so
    identical prompts only ever come from re-running the same work. `prefix` is part of the key for
    the same reason the model is — it is part of what was asked, so two calls that differ only in
    their stable half are two different questions.

    Not to be confused with the provider-side prompt cache `prefix` drives: that one lives at
    Anthropic, lasts minutes, and makes a *similar* call cheaper. This one lives in the repo's
    index, lasts until evicted, and makes an *identical* call free. They compose — a re-run of
    `specky sync` hits this cache and never reaches the network at all.

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

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        key = self.key(prefix, prompt)
        if (hit := self._lookup(key, prefix + prompt)) is not None:
            return hit
        response = self.inner.generate(prompt, prefix=prefix, task=task)
        self._store(key, prefix + prompt, response)
        return response

    def key(self, prefix: str, prompt: str) -> str:
        return hashlib.sha256(f"{self.model}\x00{prefix}\x00{prompt}".encode()).hexdigest()

    def generate_batch(
        self, prompts: dict[str, tuple[str, str]], task: str = ""
    ) -> dict[str, str]:
        """Batch, minus whatever this cache can already answer.

        The memoized entries are served first and only the misses are sent, which is what makes a
        re-run of an interrupted `--batch` backfill cheap instead of a second full batch. Results
        are stored on the way back, so the two caches stay consistent with the non-batch path.
        """
        answers = {}
        misses = {}
        for key, (prefix, prompt) in prompts.items():
            hit = self._lookup(self.key(prefix, prompt), prefix + prompt)
            if hit is not None:
                answers[key] = hit
            else:
                misses[key] = (prefix, prompt)

        if misses:
            fresh = self.inner.generate_batch(misses, task)
            for key, response in fresh.items():
                prefix, prompt = misses[key]
                self._store(self.key(prefix, prompt), prefix + prompt, response)
            answers.update(fresh)
        return answers

    def supports_batch(self, task: str = "") -> bool:
        return hasattr(self.inner, "generate_batch")

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


@dataclass
class TaskRouter:
    """Sends each call to the provider configured for its task, defaulting to the default one.

    Per-task models exist because specky's calls are not one workload. Deciding whether a commit
    touches a documented feature is a small JSON judgement that the cheapest model does well;
    working out what an unfamiliar repo *is* and which domains it has (`specky bootstrap`'s
    discovery call) is the hardest single question specky asks, it is asked once, and it sets the
    shape of every doc written afterwards. Pinning both to one model means overpaying for the
    first or underpowering the second.

    Routing lives here rather than in each caller's signature so that `provider.generate(...)` stays
    the whole interface: a call site names its task and knows nothing about models. Each task's
    provider is separately wrapped in `CachingProvider`, and the model is part of the cache key, so
    changing one task's model invalidates only that task's entries.
    """

    default: Provider
    by_task: dict[str, Provider]

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        return self.for_task(task).generate(prompt, prefix=prefix, task=task)

    def generate_batch(
        self, prompts: dict[str, tuple[str, str]], task: str = ""
    ) -> dict[str, str]:
        return self.for_task(task).generate_batch(prompts, task)

    def for_task(self, task: str) -> Provider:
        return self.by_task.get(task, self.default)

    def supports_batch(self, task: str = "") -> bool:
        provider = self.for_task(task)
        supports = getattr(provider, "supports_batch", None)
        return supports() if supports else hasattr(provider, "generate_batch")


def supports_batch(provider: Provider, task: str = "") -> bool:
    """Can this provider answer a whole batch at once (at half price)?

    False for an OpenAI-compatible endpoint and for a `command` provider, neither of which has a
    batch API specky could speak — so `--batch` degrades to the ordinary concurrent path rather
    than failing, and says so.
    """
    check = getattr(provider, "supports_batch", None)
    return check(task) if check else hasattr(provider, "generate_batch")


def _batch_id(key: str) -> str:
    """A `custom_id` the Batches API will accept for an arbitrary caller key.

    Ids are limited to 64 characters of a restricted alphabet, and specky's keys are commit shas
    (fine) and `domain/topic` pairs (not — the slash is rejected). Hashing sidesteps both limits,
    and the caller's key is recovered from a lookup table rather than from the id.
    """
    return "k" + hashlib.sha256(key.encode()).hexdigest()[:32]


def unwrap(provider: Provider) -> Provider:
    """The concrete provider behind any caching or routing wrapper.

    For code that *inspects* a provider rather than calling it — `specky doctor` reads
    `api_key_env` and `command` off it to report on the environment. Calling code should never use
    this: going around the wrapper is going around the cache and the usage log.
    """
    if isinstance(provider, TaskRouter):
        provider = provider.default
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


def task_models(config: dict) -> dict[str, str]:
    """`{"discovery": "claude-sonnet-5"}` from `[ai] <task>_model` keys.

    An unknown `<something>_model` is a ConfigError rather than a no-op: the whole point of these
    is to be set once and forgotten, so a typo that silently routes nothing would be found months
    later by reading a bill.
    """
    models = {}
    for key, value in config.items():
        if not key.endswith("_model"):
            continue
        task = key[: -len("_model")]
        if task not in TASKS:
            raise ConfigError(
                f"[ai] {key} names no task specky has — expected one of "
                + ", ".join(f"{t}_model" for t in TASKS)
            )
        if str(value).strip():
            models[task] = str(value).strip()
    return models


def load_provider_from_toml(path: Path, command: str = "") -> Provider:
    """The only construction path, so `[ai] cache`, per-task models and the usage log apply to
    every caller.

    `command` is the specky subcommand doing the asking, recorded on each usage row so `specky cost`
    can say which part of specky spent what. Each task that names its own model gets its own
    provider and its own cache namespace; everything else shares the default.
    """
    if not path.exists():
        raise ConfigError(f"{path} not found — run `specky init` first")
    with path.open("rb") as f:
        data = tomllib.load(f)
    ai_config = data.get("ai")
    if not ai_config:
        raise ConfigError(f"{path} has no [ai] table — run `specky init` first")

    cache = _flag(ai_config, "cache", True)

    def build(config: dict) -> Provider:
        provider = load_provider(config)
        if not cache:
            return provider
        return CachingProvider(provider, path.parent, _model_label(config), command)

    default = build(ai_config)
    overrides = task_models(ai_config)
    if not overrides:
        return default
    # `command` providers have no model to swap, so a per-task model there would silently do
    # nothing — better to say so than to look configured.
    if ai_config.get("provider") == "command":
        raise ConfigError(
            'per-task models need a `provider` with a model to swap; `provider = "command"` has '
            "only its command line — point the command itself at the model you want"
        )
    return TaskRouter(
        default=default,
        by_task={task: build({**ai_config, "model": model}) for task, model in overrides.items()},
    )
