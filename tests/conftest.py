"""Shared fixtures.

Two things every test here needs: a real git repo on disk (specky resolves paths via
`git rev-parse --show-toplevel` and writes its index to `<root>/.specky/index.db`), and a
stand-in for the AI provider. `generate(prompt) -> str` is the whole protocol for every caller
but one, so those stand-ins are a few lines rather than a mocking framework; `ToolProvider`
covers the exception, `specky document`, which holds a multi-turn tool conversation.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from specky.commit_doc import MICRO_DOC_PREFIX


@pytest.fixture(autouse=True)
def _no_ai_env_overrides(monkeypatch):
    """`SPECKY_AI_*` in the developer's shell would silently reconfigure every provider a test builds."""
    import os

    for name in [n for n in os.environ if n.startswith("SPECKY_AI_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _outside_any_agent_session(monkeypatch):
    """The suite runs as often from an agent's terminal as from a plain one. The env markers that
    agent sets would make every `provider = "agent"` test hand its work off to a skill."""
    from specky.ai_provider import AGENTS

    for name in {
        "AI_AGENT",
        *(spec.partition("=")[0] for agent in AGENTS.values() for spec in agent.env),
    }:
        monkeypatch.delenv(name, raising=False)


class FakeProvider:
    """Returns canned replies in order, and records every prompt it was given.

    A single string reply is repeated for every call; a list is consumed one per call.
    """

    def __init__(self, replies: str | list[str] = "ok") -> None:
        self._replies = [replies] if isinstance(replies, str) else list(replies)
        self._single = isinstance(replies, str)
        self.prompts: list[str] = []
        self.prefixes: list[str] = []

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        # `prompts` records the whole logical prompt — prefix first, exactly as a provider renders
        # it — so a test asserting that some instruction reached the model doesn't have to know
        # which half of the call it now travels in. `prefixes` is there for the tests that care
        # about the split itself.
        self.prompts.append(prefix + prompt)
        self.prefixes.append(prefix)
        if self._single:
            return self._replies[0]
        if not self._replies:
            raise AssertionError("FakeProvider ran out of canned replies")
        return self._replies.pop(0)


class RoutingProvider:
    """Answers by what the prompt asks for rather than by call order, and is thread-safe.

    `sync()` fans the micro-doc calls for a batch of commits out over a thread pool, so a
    provider that pops replies off a list would hand back whichever reply the winning thread
    reached first. This one keys off the prompt.
    """

    def __init__(
        self,
        summary: str = "A change happened.",
        classification: str = '{"skip": true}',
        doc: str = "# Doc\n\n## What It Does\nThings.\n",
    ) -> None:
        self._summary, self._classification, self._doc = summary, classification, doc
        self._lock = threading.Lock()
        self.prompts: list[str] = []
        self.prefixes: list[str] = []

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        # Routing reads the whole logical prompt, not just the volatile half: the instructions that
        # identify which kind of call this is now travel in `prefix`.
        prompt = prefix + prompt
        with self._lock:
            self.prompts.append(prompt)
            self.prefixes.append(prefix)
        if prompt.startswith(MICRO_DOC_PREFIX):
            return self._summary
        if "Respond with ONLY a JSON object" in prompt:
            return self._classification
        return self._doc


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """An initialized git repo with one commit and an empty `specs/` tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "specs").mkdir()
    (repo / "README.md").write_text("# test repo\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial commit")
    return repo


@pytest.fixture
def write_doc(tmp_repo: Path):
    """Write `specs/<rel_path>` with optional frontmatter, creating parent dirs."""

    def _write(rel_path: str, body: str, meta: dict | None = None) -> Path:
        from specky import frontmatter

        path = tmp_repo / "specs" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.render(meta or {}, body))
        return path

    return _write


class ToolProvider:
    """A provider that holds a tool conversation, driven by a script instead of a model.

    `turns` is a list of turns, each a list of `(tool name, arguments)` the "model" calls. Tools are
    reached through the same `invoke` callback the real provider loops use, so the terminal tool
    ends the run by raising through this exactly as it does in production — which is the part worth
    exercising, since neither provider catches it.
    """

    def __init__(self, turns: list[list[tuple[str, dict]]], trailing: str = "") -> None:
        self.turns = turns
        self.trailing = trailing
        self.prompts: list[str] = []
        self.prefixes: list[str] = []
        self.tasks: list[str] = []
        self.tool_names: list[str] = []
        self.turns_taken = 0

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        raise AssertionError("a tool-capable provider should never take the single-call path")

    def converse(
        self,
        prompt: str,
        *,
        prefix: str = "",
        tools,
        invoke,
        task: str = "",
        max_turns: int = 12,
        final_tool: str = "",
        on_turn=None,
    ) -> str:
        self.prompts.append(prefix + prompt)
        self.prefixes.append(prefix)
        self.tasks.append(task)
        self.tool_names = [tool.name for tool in tools]
        for turn in self.turns[:max_turns]:
            self.turns_taken += 1
            for name, arguments in turn:
                invoke(name, arguments)
            if on_turn:
                on_turn(len(prefix) + len(prompt), 0)
        return self.trailing
