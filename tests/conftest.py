"""Shared fixtures.

Two things every test here needs: a real git repo on disk (specky resolves paths via
`git rev-parse --show-toplevel` and writes its index to `<root>/.specky/index.db`), and a
stand-in for the AI provider. The `Provider` protocol is a single `generate(prompt) -> str`,
so the stand-in is a few lines rather than a mocking framework.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest


class FakeProvider:
    """Returns canned replies in order, and records every prompt it was given.

    A single string reply is repeated for every call; a list is consumed one per call.
    """

    def __init__(self, replies: str | list[str] = "ok") -> None:
        self._replies = [replies] if isinstance(replies, str) else list(replies)
        self._single = isinstance(replies, str)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
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

    def generate(self, prompt: str) -> str:
        with self._lock:
            self.prompts.append(prompt)
        if prompt.startswith("Summarize what changed"):
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
