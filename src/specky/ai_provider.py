"""AI provider abstraction: agent / anthropic / bedrock / openai-compatible / command.

Providers are configured in specky.toml under an [ai] table and constructed via
`load_provider`. Each provider exposes `generate(prompt, *, prefix, task) -> str`, where
`prefix` is the stable half of the prompt (cached where the provider supports it) and `task`
names the kind of call, which `[ai] <task>_model` can route to its own model.

Output length matters here in a way it doesn't for a chat UI: a whole feature doc
(What It Does / How It Works / Outcomes / Acceptance Tests, with tables, plus Edge Cases and a
diagram on a workflow) is produced in one
call, and a response cut off at the token limit would be written to specs/ as if it were
complete. So every provider sends an explicit `max_tokens` (DEFAULT_MAX_TOKENS, overridable
per-repo via `[ai] max_tokens`), and a truncated response raises `TruncatedResponse` rather
than returning a half-written doc.

`load_provider_from_toml` wraps whichever provider it built in `CachingProvider` unless
`[ai] cache = false`, so an identical prompt is answered from the repo's index instead of paid for
twice, and every call — hit or miss — leaves a row for `specky cost` (see cost.py).

Most of specky asks a provider one question and reads one answer. `specky document` is the
exception: it hands the model tools and lets it search the repo for itself, which is a multi-turn
conversation rather than a call. That is `converse`, kept deliberately beside `generate` rather than
replacing it — every other caller is a single call and should stay one, since a single call is what
can be cached, batched and costed exactly. Not every provider can hold a tool conversation:
`provider = "command"` is one stdin and one stdout, so it raises `ToolLoopUnsupported` and the
caller degrades (see `document.py`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

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

# How many turns one `converse` may take before it is made to finish. Each turn is a billed call,
# so this is the spend bound on a command whose cost is otherwise decided by the model. Sixteen
# rather than a tighter number because a turn is one *round*, not one tool call: a model that calls
# tools in parallel spends few, and one that calls them strictly one at a time (DeepSeek does)
# spends one per file it opens, which a thorough read of two modules exhausts on its own.
MAX_TOOL_TURNS = 16

# What is said to a model that stops calling tools without calling the terminal one. It happens for
# a mundane reason: asked to write a document, a model writes it — as prose, in the reply, instead
# of through the tool it was told to use. The text is usually the finished doc, so throwing it away
# discards the whole run. One nudge, with only the terminal tool on the table, recovers it for the
# price of a single call. It is sent at most once per run, so it cannot become a loop.
NUDGE = (
    "Stop searching now and hand the document over: call the {tool} tool with what you already "
    "have. Do not reply with the document as text — only a {tool} call is read."
)

# The tasks specky asks a model to do, each able to name its own model in `[ai]`. Kept as a tuple
# rather than free-form strings so `specky doctor` can report the routing and a typo in
# `[ai] doc_modle` is a config error rather than a setting that silently does nothing.
# `discovery` and `glossary` were here until `specky bootstrap` was removed, and they are gone
# with it rather than kept for compatibility. Leaving a name in this tuple keeps `[ai] <task>_model`
# accepting it, and an accepted key that routes nothing is the exact failure the tuple exists to
# prevent: `specky doctor` would go on reporting the route as active while nothing ever asked for
# that task. Dropping them turns a stale `discovery_model` into the loud config error it should be.
# `draft` is the Spec Assistant's draft-spec workflow (spec_draft.py): several tool-using stages per
# draft, so it is the chat task most worth routing to its own model.
TASKS = ("summary", "classify", "doc", "document", "tag", "chat", "draft")


class Provider(Protocol):
    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str: ...


class TruncatedResponse(RuntimeError):
    """The model stopped because it hit the output token limit, so the text is incomplete."""


class ToolLoopUnsupported(RuntimeError):
    """This provider has no tool channel, so it cannot run a `converse`.

    Raised by `CommandProvider`, which is one stdin and one stdout with nowhere to put a tool
    definition. It is a normal outcome rather than a failure: the caller degrades to a single
    `generate` and says so (`document.py`).
    """


# What a `converse` is handed. `tools` is only read for `.name`, `.description` and `.schema`, and
# `invoke(name, arguments) -> str` runs one and returns its result as text — so this module never
# learns what any particular tool *means*, which is what keeps `specky.tools` out of its imports and
# lets the terminal tool end the loop by raising through `invoke` (see `tools.DocSubmitted`).
if TYPE_CHECKING:
    from specky.tools import Tool

Invoke = Callable[[str, dict], str]
# Reports one turn's traffic as `(chars sent, chars received)`, so `CachingProvider` can log a usage
# row per turn. Without it a twelve-turn run would appear in `specky cost` as one call.
TurnReport = Callable[[int, int], None]


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
class _MessagesAPIProvider:
    """Claude's Messages API, whichever door it's reached through — Anthropic's own endpoint or
    Amazon Bedrock. The request shape, caching and tool loop are identical; only the client (how it
    authenticates, where it points) and the model IDs differ, so `_client` is all a subclass adds.
    """

    model: str = "claude-haiku-4-5"
    max_tokens: int = DEFAULT_MAX_TOKENS

    def _client(self):
        raise NotImplementedError

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        client = self._client()
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

    def converse(
        self,
        prompt: str,
        *,
        prefix: str = "",
        tools: list["Tool"],
        invoke: Invoke,
        task: str = "",
        max_turns: int = MAX_TOOL_TURNS,
        final_tool: str = "",
        on_turn: TurnReport | None = None,
    ) -> str:
        """Let the model use tools until it stops, and return whatever it said last.

        The terminal condition is not this method's business. A tool that ends the run does so by
        raising through `invoke`, which unwinds straight past this loop to the caller — so a
        `converse` that returns normally means the model ran out of turns or simply stopped talking,
        both of which are the caller's problem to report (`document.py`).

        `prefix` keeps the cache breakpoint `generate` gives it, and it matters much more here: the
        instructions, the doc template and the list of every existing doc are resent on *every turn*
        of every run, so on a twelve-turn conversation that block is read thirteen times and paid
        for once.
        """
        # A tool conversation is many sequential calls, and the last of them is the one that writes
        # a whole doc — so the per-call timeout is the same 60s every other call gets, but the run
        # as a whole is bounded by `max_turns` rather than by the clock.
        client = self._client()

        specs = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.schema}
            for tool in tools
        ]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        sent = len(prefix) + len(prompt)
        nudged = False

        for turn in range(max_turns):
            # A forced turn offers *only* the terminal tool, not merely a preference for it. Left
            # with the full list, a model asked to finish will sometimes reach for one more read
            # instead — observed against a real endpoint, which picked a search over the tool it
            # had been told to call.
            forcing = bool(final_tool) and (nudged or turn == max_turns - 1)
            kwargs: dict = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
                "tools": [s for s in specs if s["name"] == final_tool] if forcing else specs,
            }
            if prefix:
                kwargs["system"] = [
                    {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
                ]
            if forcing:
                kwargs["tool_choice"] = {"type": "tool", "name": final_tool}

            response = client.messages.create(**kwargs)
            said = "".join(block.text for block in response.content if block.type == "text")
            if on_turn:
                on_turn(sent, len(said))
            if response.stop_reason == "max_tokens":
                raise TruncatedResponse(
                    f"{self.model} hit the {self.max_tokens}-token output limit — "
                    "raise `max_tokens` in specky.toml's [ai] table"
                )
            if response.stop_reason != "tool_use":
                # Stopped talking without finishing. Nudge once; if it has already been nudged, it
                # means it twice and whatever it said is all there is.
                if not final_tool or nudged or turn >= max_turns - 1:
                    return said
                nudged = True
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": NUDGE.format(tool=final_tool)})
                sent += len(said) + len(NUDGE)
                continue

            messages.append({"role": "assistant", "content": response.content})
            sent += len(said)
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                # `invoke` contains its own failures and returns them as text — except the terminal
                # tool, which raises through here on purpose and ends the run.
                result = invoke(block.name, dict(block.input))
                sent += len(result)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )
            messages.append({"role": "user", "content": results})

        return ""


@dataclass
class AnthropicProvider(_MessagesAPIProvider):
    api_key_env: str = "ANTHROPIC_API_KEY"

    def _client(self):
        import anthropic

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")
        return anthropic.Anthropic(api_key=api_key, timeout=DEFAULT_TIMEOUT_SECONDS)

    def generate_batch(
        self, prompts: dict[str, tuple[str, str]], task: str = ""
    ) -> dict[str, str]:
        """Answer many independent prompts through the Message Batches API, at half the price.

        `prompts` maps a caller's own key to `(prefix, prompt)`; the result maps the same keys to
        answers. A key whose request failed is absent rather than raising — the caller decides
        whether one bad commit should stop a backfill, and every caller here decides it shouldn't.

        Worth it only for work nobody is waiting on: a batch is asynchronous and the API allows up
        to 24 hours, so this is reached for by `specky sync --batch` and never by a git hook, which
        must not turn `git commit` into a long poll.
        """
        client = self._client()

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


# What `uv tool install` needs for the Bedrock client's request signing — an optional extra, so a
# user who never touches AWS doesn't install boto3.
BEDROCK_INSTALL_HINT = "uv tool install 'specky[bedrock]' --force"


@dataclass
class BedrockProvider(_MessagesAPIProvider):
    """Claude through Amazon Bedrock's Messages API (the SDK's Mantle client).

    Credentials come from the standard AWS chain — env vars, `aws_profile`, an instance or task
    role — so specky names no secret here either. No `generate_batch`: Bedrock has no Message
    Batches API, so `--batch` degrades to ordinary calls, as it does for every non-Anthropic
    provider. Model IDs carry Bedrock's `anthropic.` prefix.
    """

    model: str = "anthropic.claude-haiku-4-5"
    aws_region: str = ""
    aws_profile: str = ""

    def _client(self):
        import importlib.util

        # The SDK builds a Bedrock client without botocore and only fails on the first request,
        # with a bare ModuleNotFoundError; saying which install fixes it is kinder.
        if importlib.util.find_spec("botocore") is None:
            raise RuntimeError(
                f"the bedrock provider needs the AWS SDK, which isn't installed — run "
                f"`{BEDROCK_INSTALL_HINT}`"
            )
        try:
            from anthropic import AnthropicBedrockMantle
        except ImportError:
            raise RuntimeError(
                "this anthropic SDK predates Bedrock's Messages API client — run "
                f"`{BEDROCK_INSTALL_HINT}` to upgrade it"
            ) from None

        return AnthropicBedrockMantle(
            aws_region=self.aws_region or None,
            aws_profile=self.aws_profile or None,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )


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

    def converse(
        self,
        prompt: str,
        *,
        prefix: str = "",
        tools: list["Tool"],
        invoke: Invoke,
        task: str = "",
        max_turns: int = MAX_TOOL_TURNS,
        final_tool: str = "",
        on_turn: TurnReport | None = None,
    ) -> str:
        """The same loop as `AnthropicProvider.converse`, in OpenAI's spelling.

        "OpenAI-compatible" is a claim about the chat-completions shape, and tool calling is the
        part of it endpoints most often leave out — Ollama, vLLM, llama.cpp and the various hosted
        gateways all differ. So a rejection that mentions tools is turned into a `ConfigError`
        naming the lever, rather than an httpx stack trace that reads like a specky bug.
        """
        import httpx

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set in the environment")

        specs = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema,
                },
            }
            for tool in tools
        ]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        if prefix:
            messages.insert(0, {"role": "system", "content": prefix})
        sent = len(prefix) + len(prompt)
        nudged = False

        for turn in range(max_turns):
            forcing = bool(final_tool) and (nudged or turn == max_turns - 1)
            payload: dict = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
                # Only the terminal tool on a forced turn — see the note in the Anthropic loop.
                "tools": [s for s in specs if s["function"]["name"] == final_tool]
                if forcing
                else specs,
            }
            if forcing:
                payload["tool_choice"] = {"type": "function", "function": {"name": final_tool}}

            response = httpx.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            if response.status_code in (400, 404, 422) and "tool" in response.text.lower():
                raise ConfigError(
                    f"{self.base_url} rejected a tool-calling request ({response.status_code}), so "
                    f"`specky document` can't run against {self.model}. Point [ai] at an endpoint "
                    "that supports tool use, or at provider = \"anthropic\""
                )
            response.raise_for_status()
            choice = response.json()["choices"][0]
            message = choice["message"]
            said = message.get("content") or ""
            if on_turn:
                on_turn(sent, len(said))
            if choice.get("finish_reason") == "length":
                raise TruncatedResponse(
                    f"{self.model} hit the {self.max_tokens}-token output limit — "
                    "raise `max_tokens` in specky.toml's [ai] table"
                )

            calls = message.get("tool_calls") or []
            if not calls:
                # See the Anthropic loop: a model asked to write a document often writes it as
                # prose instead of calling the tool, and discarding that throws the run away.
                if not final_tool or nudged or turn >= max_turns - 1:
                    return said
                nudged = True
                messages.append(message)
                messages.append({"role": "user", "content": NUDGE.format(tool=final_tool)})
                sent += len(said) + len(NUDGE)
                continue

            messages.append(message)
            sent += len(said)
            for call in calls:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    # Handed back as the tool's result rather than raised: the model wrote the
                    # arguments, it can see they didn't parse, and the next turn usually fixes it.
                    arguments, result = {}, f"arguments were not valid JSON: {exc}"
                else:
                    result = invoke(function.get("name", ""), arguments)
                sent += len(result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )

        return ""


class CommandFailed(subprocess.CalledProcessError):
    """A provider command's non-zero exit, with the reason it printed.

    Still a `CalledProcessError`, for the callers that catch one. The default message says only
    "returned non-zero exit status 1", which for an agent CLI hides the one line that matters —
    `Error: Login canceled` from a `devin -p` that isn't logged in.
    """

    def __str__(self) -> str:
        detail = (self.stderr or "").strip() or (self.output or "").strip()
        lines = detail.splitlines()[-5:]
        return f"`{shlex.join(self.cmd)}` exited {self.returncode}" + (
            ": " + " / ".join(lines) if lines else ""
        )


@dataclass
class CommandProvider:
    command: str

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        # An arbitrary CLI has one input channel, so the split is undone here: the command sees
        # exactly the prompt it would have seen before `prefix` existed. Whether anything is cached
        # is then the command's own business — `claude -p` does its own prompt caching, and specky
        # can neither help nor measure it.
        argv = shlex.split(self.command)
        result = subprocess.run(
            argv,
            input=f"{prefix}\n\n{prompt}" if prefix else prompt,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise CommandFailed(result.returncode, argv, result.stdout, result.stderr)
        return result.stdout.strip()

    def converse(self, prompt: str, **kwargs) -> str:
        """Never. One stdin, one stdout, nowhere to put a tool definition.

        Worth being precise about why, because the command is often an agent that plainly *does*
        have tools: `claude -p` can read and grep, but through its own harness, invisibly to specky
        — which can neither offer the tools in `tools.py` nor see which files were opened. So the
        caller degrades to a single `generate` and loses the guard that a doc must be written from
        code someone actually read (`document.py`).
        """
        raise ToolLoopUnsupported(
            f"provider = \"command\" ({self.command}) has no tool channel, so specky can't drive "
            "a search loop through it"
        )


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentCLI:
    """A coding agent specky can run headless: prompt on stdin, answer on stdout.

    `provider = "agent"` is `command` with the command line written for you — and, unlike a
    hand-written one, with the model as a real setting, so `model` and `<task>_model` work. The
    argv is `head + [model_flag, model] + tail`, because some CLIs (`codex exec -`) need the stdin
    marker last.
    """

    label: str
    head: tuple[str, ...]
    model_flag: str = "--model"
    tail: tuple[str, ...] = ()
    # Session markers: env vars the agent sets for the commands it runs, so specky launched from
    # inside a session (the setup skill, a terminal tab, a git hook) can tell which agent that
    # is. `VAR` matches on presence; `VAR=substring` also requires the value to contain it.
    env: tuple[str, ...] = ()

    @property
    def executable(self) -> str:
        return self.head[0]

    def argv(self, model: str = "") -> list[str]:
        return [*self.head, *((self.model_flag, model) if model else ()), *self.tail]


# Ordered by preference, for when no session says which agent is the current one.
AGENTS: dict[str, AgentCLI] = {
    "claude": AgentCLI("Claude Code", ("claude", "-p"), env=("CLAUDECODE",)),
    "codex": AgentCLI(
        "Codex", ("codex", "exec", "--skip-git-repo-check"), tail=("-",), env=("CODEX_SANDBOX",)
    ),
    "gemini": AgentCLI("Gemini CLI", ("gemini", "-p", ""), env=("GEMINI_CLI",)),
    "opencode": AgentCLI("opencode", ("opencode", "run"), env=("OPENCODE",)),
    "kiro": AgentCLI("Kiro", ("kiro-cli", "chat", "--no-interactive")),
    "cursor": AgentCLI("Cursor Agent", ("cursor-agent", "-p")),
    # `devin -p` ignores stdin, so the prompt comes in as a file. Print mode can't show the
    # workspace-trust prompt and fails in an untrusted directory — a fresh clone, a Devin VM.
    # Devin sets no variable of its own, but Devin Desktop is a VS Code fork whose shells
    # inherit `VSCODE_IPC_HOOK` pointing into the app's data directory —
    # `…/Application Support/Devin/…` where VS Code's says Code and Windsurf's says Windsurf —
    # so the socket's path is the marker.
    "devin": AgentCLI(
        "Devin",
        ("devin", "-p", "--prompt-file", "/dev/stdin", "--respect-workspace-trust", "false"),
        env=("VSCODE_IPC_HOOK=/Devin/",),
    ),
}


def current_agent(environ: Mapping[str, str] | None = None) -> str | None:
    """The coding agent specky should run, or None if there's none on PATH.

    The one whose session this is, when its env says so — its own marker, or the cross-agent
    `AI_AGENT` (`claude-code_2-1-280_agent`) — else the first installed, in `AGENTS` order.
    """
    environ = os.environ if environ is None else environ
    installed = [name for name, agent in AGENTS.items() if shutil.which(agent.executable)]
    for name in installed:
        if in_agent_session(name, environ):
            return name
    return installed[0] if installed else None


def in_agent_session(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process runs inside a session of agent `name`, by the env that agent sets.

    A git hook inherits the env of the shell that ran `git commit`, so a commit an agent makes
    carries its marker into `specky commit-doc`. An agent with no marker at all (Kiro, Cursor)
    never matches.
    """
    environ = os.environ if environ is None else environ
    agent = AGENTS.get(name)
    if agent is None:
        return False

    def marked(spec: str) -> bool:
        var, sep, want = spec.partition("=")
        value = environ.get(var)
        return bool(value) if not sep else bool(value) and want in value

    marker = environ.get("AI_AGENT", "").lower()
    return any(marked(spec) for spec in agent.env) or bool(
        marker and marker.startswith(name)
    )


def skill_handoff(config: Mapping, environ: Mapping[str, str] | None = None) -> bool:
    """Whether specky should leave this work to the session agent's skills instead of launching a
    headless copy of that same agent.

    Only `provider = "agent"`, only from inside that agent's own session, and only while
    `[ai] skill_handoff` isn't turned off. Every other provider, and the agent outside a session
    (a terminal commit, a rebase, CI), keeps the headless path.
    """
    if config.get("provider") != "agent" or not _flag(config, "skill_handoff", True):
        return False
    return in_agent_session(str(config.get("agent", "")), environ)


# The first words of the line `commit-doc` prints when it hands commits to the session agent.
# The Claude Code hook looks for it to lift the line into the agent's context.
HANDOFF_MARKER = "specky: commits to document"


def agent_command(name: str, model: str = "") -> str:
    """The shell command `provider = "agent"` runs, for `CommandProvider` and for display."""
    if name not in AGENTS:
        raise ConfigError(
            f"[ai] agent = {name!r} isn't one specky knows — expected one of {', '.join(AGENTS)}, "
            'or use provider = "command" with the command line spelled out'
        )
    return shlex.join(AGENTS[name].argv(model))




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

    def converse(self, prompt: str, *, on_turn=None, **kwargs) -> str:
        """Logged, never memoized — and the asymmetry with `generate` is deliberate.

        The key is `sha256(model + prefix + prompt)`, which is a complete description of a
        single-call question and a badly incomplete one of a tool conversation: the answer also
        depends on every file the model chose to read, and those aren't in the key. So a cache hit
        after the code changed would serve a doc describing the repo as it used to be — silently,
        and exactly on the re-runs where someone is checking whether their edit is reflected.

        Usage rows still land, one per turn, so `specky cost` reports a twelve-turn run as twelve
        calls rather than one. They are all `cached=0`, which is the truth.
        """

        def record(sent: int, received: int) -> None:
            if on_turn:
                on_turn(sent, received)
            try:
                conn = connect(self.repo_root)
            except sqlite3.Error:
                return
            try:
                conn.execute(
                    "INSERT INTO usage (created_at, command, model, prompt_chars, response_chars, "
                    "cached) VALUES (?, ?, ?, ?, ?, 0)",
                    (_now(), self.command, self.model, sent, received),
                )
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                conn.close()

        return self.inner.converse(prompt, on_turn=record, **kwargs)

    def supports_tools(self, task: str = "") -> bool:
        # Takes `task` it never reads, to match `supports_batch` below. The asymmetry is not free:
        # the module-level helper had to call this inside `try/except TypeError`, which also
        # swallowed any TypeError raised *inside* a provider's own implementation.
        return hasattr(self.inner, "converse") and not isinstance(self.inner, CommandProvider)

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
    working out which files across a whole repo make up a feature and then writing its doc
    (`specky document`'s tool conversation) is the hardest thing specky asks of a model, and the
    only task where a weak one shows up directly in what lands in `specs/`. Pinning both to one
    model means overpaying for the first or underpowering the second.

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

    def converse(self, prompt: str, *, task: str = "", **kwargs) -> str:
        return self.for_task(task).converse(prompt, task=task, **kwargs)

    def for_task(self, task: str) -> Provider:
        return self.by_task.get(task, self.default)

    def supports_batch(self, task: str = "") -> bool:
        provider = self.for_task(task)
        supports = getattr(provider, "supports_batch", None)
        return supports() if supports else hasattr(provider, "generate_batch")

    def supports_tools(self, task: str = "") -> bool:
        return supports_tools(self.for_task(task))


def supports_batch(provider: Provider, task: str = "") -> bool:
    """Can this provider answer a whole batch at once (at half price)?

    False for an OpenAI-compatible endpoint and for a `command` provider, neither of which has a
    batch API specky could speak — so `--batch` degrades to the ordinary concurrent path rather
    than failing, and says so.
    """
    check = getattr(provider, "supports_batch", None)
    return check(task) if check else hasattr(provider, "generate_batch")


def supports_tools(provider: Provider, task: str = "") -> bool:
    """Can this provider hold a tool conversation?

    False only for `command`, which has no tool channel at all. `specky document` asks before it
    starts so it can announce the degraded path up front, rather than building a toolbox and a
    prompt and then discovering there is nowhere to send them.
    """
    check = getattr(provider, "supports_tools", None)
    if check is not None:
        return check(task)
    return hasattr(provider, "converse") and not isinstance(provider, CommandProvider)


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
    if kind == "bedrock":
        return BedrockProvider(
            model=config.get("model", "anthropic.claude-haiku-4-5"),
            aws_region=config.get("aws_region", ""),
            aws_profile=config.get("aws_profile", ""),
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
    if kind == "agent":
        if not config.get("agent"):
            raise ConfigError('[ai] provider="agent" requires \'agent\'')
        return CommandProvider(command=agent_command(config["agent"], config.get("model", "")))
    if kind == "command":
        if not config.get("command"):
            raise ConfigError('[ai] provider="command" requires \'command\'')
        return CommandProvider(command=config["command"])
    raise ConfigError(f"Unknown [ai] provider: {kind!r} (expected agent/anthropic/bedrock/openai-compatible/command)")


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
    if config.get("provider") == "agent":
        return f"agent:{config.get('agent', '')}:{config.get('model', '')}"
    return str(config.get("model", ""))


def task_models(config: dict) -> dict[str, str]:
    """`{"document": "claude-sonnet-5"}` from `[ai] <task>_model` keys.

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


# `[ai]` from the environment, for a deployed `specky serve`: a container is built from the repo,
# and specky.toml is gitignored, so it has no file to read — and a server usually wants its own
# models anyway (`SPECKY_AI_DRAFT_MODEL` for the Spec Assistant's drafts). Same shape as
# `SPECKY_AUTH_*`: configuration a host's env settings can hold.
AI_ENV_PREFIX = "SPECKY_AI_"


def ai_env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`{"draft_model": "…"}` from `SPECKY_AI_DRAFT_MODEL=…`, empty values ignored."""
    environ = os.environ if environ is None else environ
    return {
        name[len(AI_ENV_PREFIX) :].lower(): value.strip()
        for name, value in environ.items()
        if name.startswith(AI_ENV_PREFIX) and len(name) > len(AI_ENV_PREFIX) and value.strip()
    }


def read_ai_config(path: Path, environ: Mapping[str, str] | None = None) -> dict:
    """The `[ai]` table in effect: specky.toml's, with `SPECKY_AI_*` applied on top.

    `SPECKY_AI_PROVIDER` replaces the table outright rather than patching it, because the file's
    keys belong to the file's provider: a local `agent = "claude"`, `model = "opus"` carried into a
    server's DeepSeek config would be a model name DeepSeek has never heard of. Without it, each
    variable overrides its one key. Either way, env alone is enough — no file needed.
    """
    overrides = ai_env_overrides(environ)
    if "provider" in overrides:
        return overrides
    if not path.exists():
        raise ConfigError(f"{path} not found — run `specky init` first")
    with path.open("rb") as f:
        table = tomllib.load(f).get("ai")
    if not table:
        raise ConfigError(f"{path} has no [ai] table — run `specky init` first")
    return {**table, **overrides}


def load_provider_from_toml(path: Path, command: str = "") -> Provider:
    """The only construction path, so `[ai] cache`, per-task models and the usage log apply to
    every caller.

    `command` is the specky subcommand doing the asking, recorded on each usage row so `specky cost`
    can say which part of specky spent what. Each task that names its own model gets its own
    provider and its own cache namespace; everything else shares the default.
    """
    ai_config = read_ai_config(path)

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
