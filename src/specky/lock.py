"""One doc-writing run at a time, per repo.

Needed because a hook fire reconciles a *backlog* rather than one commit (see commit_doc's module
docstring). Two fires that overlap — `git commit` twice in quick succession, an agent committing
while someone runs `specky sync` in another terminal, a `post-merge` and a `post-commit` landing
together — would otherwise both read the same pending list, both pay for the same AI call, and both
try to `git add`/`git commit` the result. Git's own index lock makes the second one of those fail
in a way the user can do nothing about.

Deliberately non-blocking. A git hook that waits is a git hook that hangs a commit, so the loser
says so and exits successfully: whatever it skipped is still pending, and the next fire (or a
`specky sync`) picks it up. That's the same "the backlog is the source of truth" property the rest
of the flow relies on.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # POSIX only; specky's hooks are /bin/sh scripts, but the library shouldn't assume it
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

LOCK_NAME = "run.lock"


class LockBusy(RuntimeError):
    """Another specky run holds this repo's lock."""


@contextmanager
def exclusive(repo_root: Path) -> Iterator[None]:
    """Hold this repo's write lock, or raise `LockBusy` at once if someone else does.

    The lock file lives in `.specky/`, which is gitignored, and is never deleted: unlinking it
    would let a second process create a *different* file with the same name and take a lock on
    that instead. An empty file is the cheapest correct token.
    """
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return

    lock_dir = repo_root / ".specky"
    lock_dir.mkdir(exist_ok=True)
    handle = (lock_dir / LOCK_NAME).open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusy(
                "another specky run is writing docs in this repo — skipping (nothing is lost, "
                "the backlog is picked up by the next run or by `specky sync`)"
            ) from exc
        yield
    finally:
        handle.close()  # closing the descriptor releases the flock
