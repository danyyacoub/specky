"""Per-commit micro-doc generation, called by the git post-commit hook.

This is deliberately a real git hook rather than an agent-specific one (Claude Code
PostToolUse, opencode tool.execute.after, Kiro agent hooks) — it must fire on every
commit regardless of which agent (or no agent) made it. Each commit gets a real
markdown file under specs/history/, plus a mirrored row in the micro_docs sqlite
table for Phase 3's indexer to pick up.

Deliberately post-commit, not pre-commit/commit-msg: the history doc's filename is keyed
by the commit SHA, which doesn't exist until the commit object is created, and doc
generation calls an AI provider that must never be able to block or fail a commit (see
the "Generation fails -> Commit succeeds" guarantee in
specs/documentation/auto-commit-docs.md). `main()` commits whatever it writes under
specs/ as a second, separate commit rather than leaving it as a dangling uncommitted
change — guarded by `_AUTO_COMMIT_MARKER` so that follow-up commit's own post-commit
firing doesn't recurse.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from specky import frontmatter
from specky.ai_provider import ConfigError, Provider, load_provider_from_toml
from specky.db import connect, repo_root as _repo_root

if TYPE_CHECKING:  # generator imports from this module, so it can only be imported lazily here
    from specky.generator import ExistingDocs

POST_COMMIT_HOOK = """#!/bin/sh
# Installed by `specky install-git-hook`. Records a short AI micro-doc for this commit.
command -v specky >/dev/null 2>&1 && specky commit-doc || true
"""

HOOK_MARKER = "specky commit-doc"

# Prefix for the follow-up commit `main()` makes for whatever it writes under specs/. Checked
# at the top of `main()` so that commit's own post-commit firing recognizes itself and returns
# immediately instead of generating a doc *for* the doc-sync commit and recursing forever.
_AUTO_COMMIT_MARKER = "docs: sync specky docs [skip specky]"

# Diffs are truncated to this many characters before going into a prompt — long enough for
# context, short enough to keep prompt cost/latency predictable regardless of commit size.
DIFF_TRUNCATE_CHARS = 8000

# How many commits' micro-doc summaries `sync()` asks for at once, and the commit count above
# which it stops to confirm first. Adopting specky on an existing repo means one `specky sync`
# over its whole history — hundreds of billable calls — so that has to be a deliberate yes.
SYNC_CONCURRENCY = 4
SYNC_CONFIRM_THRESHOLD = 25


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    message: str
    diff: str


def _commit_info(rev: str = "HEAD") -> Commit:
    sha, author, date, message = subprocess.run(
        ["git", "log", "-1", "--format=%H%x1f%an <%ae>%x1f%aI%x1f%B", rev],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\x1f", 3)
    diff = subprocess.run(
        ["git", "show", "--format=", sha], capture_output=True, text=True, check=True
    ).stdout
    return Commit(sha=sha, author=author, date=date, message=message.strip(), diff=diff)


def generate_micro_doc(commit: Commit, provider: Provider) -> str:
    prompt = (
        "Summarize what changed and why, in one short paragraph, for a commit history reader.\n\n"
        f"Commit message:\n{commit.message}\n\nDiff (may be truncated):\n{commit.diff[:DIFF_TRUNCATE_CHARS]}"
    )
    return provider.generate(prompt).strip()


def _recorded_sha(path: Path) -> str | None:
    """The full sha a history doc says it documents, or None for one written before that was
    recorded (the filename's 8-hex prefix is all those carry)."""
    recorded = frontmatter.parse(path.read_text())[0].get("sha")
    return recorded if isinstance(recorded, str) else None


def history_doc_for(history_dir: Path, sha: str) -> Path | None:
    """The doc that documents `sha`, or None if this commit still needs one.

    Two names are possible, because 8 hex digits is not a unique key on a large repo: the usual
    `<sha8>.md`, and the `<sha12>.md` written when some other commit got there first. A doc with
    no `sha:` is taken at its filename, which is the best that can be said for one written before
    the full sha was recorded.
    """
    for name in (f"{sha[:8]}.md", f"{sha[:12]}.md"):
        path = history_dir / name
        if path.exists() and _recorded_sha(path) in (None, sha):
            return path
    return None


def write_history_file(repo_root: Path, commit: Commit, summary: str) -> Path:
    history_dir = repo_root / "specs" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    path = history_dir / f"{commit.sha[:8]}.md"
    if path.exists() and _recorded_sha(path) not in (None, commit.sha):
        path = history_dir / f"{commit.sha[:12]}.md"  # that name is another commit's

    # The full sha is what `history_doc_for` matches on; the body keeps showing the short one,
    # which is what a reader wants to see and copy.
    path.write_text(
        frontmatter.render(
            {"sha": commit.sha},
            f"# Commit {commit.sha[:8]}\n\n"
            f"- **Date:** {commit.date}\n"
            f"- **Author:** {commit.author}\n"
            f"- **Message:** {commit.message.splitlines()[0]}\n\n"
            f"{summary}\n",
        )
    )
    return path


def record_micro_doc(repo_root: Path, commit: Commit, summary: str) -> None:
    conn = connect(repo_root)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO micro_docs (sha, summary, created_at) VALUES (?, ?, ?)",
            (commit.sha, summary, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def record_commit_link(repo_root: Path, sha: str, doc_rel_path: str) -> None:
    conn = connect(repo_root)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO commit_links (sha, path) VALUES (?, ?)", (sha, doc_rel_path)
        )
        conn.commit()
    finally:
        conn.close()


def _is_revision(repo_root: Path, value: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
        ).returncode
        == 0
    )


def pending_commits(
    repo_root: Path,
    since: str | None = None,
    limit: int | None = None,
    all_branches: bool = False,
) -> list[tuple[str, str]]:
    """(sha, subject) for every commit still needing a doc, oldest first.

    Skipped: commits that already have a history doc (see `history_doc_for`), and specky's own
    doc-sync commits — `main()` refuses to document those when the hook fires, and a backfill has
    no business paying to document them either.

    `since` accepts either form a user is likely to reach for: a revision (`v1.2.0`, `HEAD~50`,
    a sha) becomes `<since>..HEAD`, and anything else is handed to git as `--since=<date>`
    ("2 weeks ago", "2026-01-01"). `limit` caps the result *after* filtering, so `--limit 5`
    means five commits actually processed, not five inspected.

    `all_branches` walks every ref instead of just `HEAD`, so work that only exists on a side
    branch gets documented too. Opt-in, because it multiplies the commit count — and therefore
    the number of billable calls — that `_confirm` exists to make deliberate.
    """
    history_dir = repo_root / "specs" / "history"

    # `git log` rather than `rev-list` for the subject line, which the progress and --dry-run
    # output both want; the revision walking is identical.
    args = ["git", "log", "--reverse", "--format=%H%x1f%s"]
    tip = ["--all"] if all_branches else ["HEAD"]
    if since and _is_revision(repo_root, since):
        # `--all --not <rev>` is the multi-ref spelling of `<rev>..HEAD`.
        args += [*tip, "--not", since] if all_branches else [f"{since}..HEAD"]
    elif since:
        args += [f"--since={since}", *tip]
    else:
        args += tip

    log = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, check=True).stdout
    pending = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\x1f")
        if subject.startswith(_AUTO_COMMIT_MARKER) or history_doc_for(history_dir, sha):
            continue
        pending.append((sha, subject))
    return pending[:limit] if limit else pending


def _sync_one(
    repo_root: Path,
    commit: Commit,
    provider: Provider,
    existing: ExistingDocs | None = None,
    label: str = "specky commit-doc",
    summary: str | None = None,
) -> list[Path]:
    """History log entry (changelog trail) + feature/workflow reference doc, if this commit
    affects one. The specs/<domain>/<topic>.md docs are the reference; specs/history/ is just
    the supplementary per-commit trail alongside them.

    `existing` lets a multi-commit caller (`sync()`) reuse one walk of specs/ across every
    commit; the single-commit hook path leaves it out. `label` prefixes this commit's output —
    `sync()` passes a `[12/431] abc1234` progress marker. `summary` is the micro-doc when the
    caller already fetched it (see `_prefetch_summaries`)."""
    from specky.generator import sync_feature_doc

    if summary is None:
        summary = generate_micro_doc(commit, provider)
    history_path = write_history_file(repo_root, commit, summary)
    record_micro_doc(repo_root, commit, summary)
    print(f"{label}: wrote {history_path}")
    written = [history_path]

    # A doc can be linked without being written — see generator.DocSync. `written` is what gets
    # committed, so a refused or frozen doc stays out of it while the link, which answers "which
    # doc covers this commit", is recorded either way.
    result = sync_feature_doc(repo_root, commit, provider, existing)
    if result:
        print(f"{label}: {result.note}")
        if result.written:
            written.append(result.path)
        record_commit_link(repo_root, commit.sha, str(result.path.relative_to(repo_root)))
    return written


def _prefetch_summaries(
    commits: list[Commit], provider: Provider
) -> dict[str, Future[str]]:
    """Ask for a batch of commits' micro-doc summaries concurrently.

    Only this call is parallelised, and deliberately so. A commit's micro-doc depends on nothing
    but that commit, whereas classification is fed the running `ExistingDocs` snapshot — run
    those concurrently and every commit in a batch is told the same (stale) list of documented
    features, which is exactly how a backfill ends up with three docs about one subject. So:
    summaries fan out, classification and every write stay serial and in commit order.

    Returns unresolved futures on purpose. The pool has already finished by the time this
    returns, so `.result()` is instant — but calling it inside the caller's per-commit
    try/except is what keeps one provider error from taking down the whole batch.
    """
    with ThreadPoolExecutor(max_workers=SYNC_CONCURRENCY) as pool:
        return {c.sha: pool.submit(generate_micro_doc, c, provider) for c in commits}


def _call_estimate(commits: int) -> str:
    """Two provider calls per commit (micro-doc + classification), plus a third for each commit
    that turns out to affect a documented feature — hence a range, not a number."""
    return f"~{2 * commits}-{3 * commits} AI calls"


def _confirm(count: int, assume_yes: bool) -> None:
    if assume_yes or count < SYNC_CONFIRM_THRESHOLD:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{count} commits ({_call_estimate(count)}) is over the {SYNC_CONFIRM_THRESHOLD}-commit "
            "confirmation threshold and stdin isn't a terminal — re-run with --yes, or narrow it "
            "with --since/--limit"
        )
    answer = input(f"specky sync: {count} commits, {_call_estimate(count)}. Continue? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise RuntimeError("cancelled")


def sync(
    since: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    all_branches: bool = False,
) -> list[Path]:
    """Generate a micro-doc + feature/workflow doc update for every commit that doesn't have a
    history entry yet. Idempotent for the history log — safe to re-run any time (e.g. after
    installing specky on a repo with existing history, or after a commit the post-commit hook
    missed because `specky` wasn't on PATH yet). Feature docs may be updated again on a re-sync
    if a later run reclassifies the same commit differently; the history log is what gates which
    commits get (re-)processed at all.

    A commit whose generation fails is reported and skipped, not fatal — one bad diff (or one
    transient provider error) shouldn't abandon a backfill of several hundred commits that's
    already half done.

    `since`/`limit`/`all_branches` choose the range (see `pending_commits`), `dry_run` lists what
    would be processed without contacting the provider at all, and `assume_yes` skips the
    confirmation that a large backfill otherwise stops for.
    """
    from specky.generator import ExistingDocs

    repo_root = _repo_root()
    pending = pending_commits(repo_root, since=since, limit=limit, all_branches=all_branches)
    total = len(pending)
    if not pending:
        print("specky sync: already up to date")
        return []

    if dry_run:
        print(f"specky sync: {total} commits to document, {_call_estimate(total)}")
        for i, (sha, subject) in enumerate(pending, 1):
            print(f"  [{i}/{total}] {sha[:8]} {subject}")
        return []

    _confirm(total, assume_yes)
    provider = load_provider_from_toml(repo_root / "specky.toml", "sync")  # let ConfigError surface

    # One walk of specs/ for the whole backfill; sync_feature_doc folds each doc it writes back
    # into it, so commit 400 is told about the doc commit 3 created.
    existing = ExistingDocs.load(repo_root)

    written: list[Path] = []
    for start in range(0, total, SYNC_CONCURRENCY):
        batch = [_commit_info(sha) for sha, _ in pending[start : start + SYNC_CONCURRENCY]]
        summaries = _prefetch_summaries(batch, provider)
        for offset, commit in enumerate(batch):
            label = f"[{start + offset + 1}/{total}] {commit.sha[:8]}"
            try:
                written += _sync_one(
                    repo_root,
                    commit,
                    provider,
                    existing,
                    label=label,
                    summary=summaries[commit.sha].result(),
                )
            except Exception as exc:
                print(f"{label}: skipped ({exc})")
    print(f"specky sync: wrote {len(written)} files across {total} commits")
    return written


def _commit_doc_updates(repo_root: Path) -> None:
    """Stage and commit whatever `_sync_one` just wrote under specs/, as its own commit —
    covers the history file, any feature/workflow doc, and update_modules_index()'s
    best-effort MODULES.md edit (generator.py) without having to track each path. Best-effort
    like the rest of this module: a failure here (e.g. another hook rejects the commit) is
    printed, not raised — the original commit already succeeded and must stay that way."""
    try:
        subprocess.run(["git", "add", "specs"], cwd=repo_root, check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
        if staged.returncode == 0:
            return  # nothing written this run (e.g. commit was skipped by classification)
        subprocess.run(["git", "commit", "-m", _AUTO_COMMIT_MARKER], cwd=repo_root, check=True)
        print("specky commit-doc: committed doc updates")
    except subprocess.CalledProcessError as exc:
        print(f"specky commit-doc: doc updates written but not committed ({exc})")


def main() -> None:
    """post-commit hook entry point. Never raises: the commit it's documenting has already
    landed, so every failure mode here — bad config, provider down, unparseable response — is
    a printed line and a clean exit. The installed hook's `|| true` is a second belt; this is
    the actual guarantee (see the module docstring)."""
    try:
        repo_root = _repo_root()
        commit = _commit_info("HEAD")
        if commit.message.startswith(_AUTO_COMMIT_MARKER):
            return  # this commit *is* our own doc-sync commit from below — don't recurse

        try:
            provider = load_provider_from_toml(repo_root / "specky.toml", "commit-doc")
        except ConfigError as exc:
            print(f"specky commit-doc: skipping ({exc})")
            return

        _sync_one(repo_root, commit, provider)
        _commit_doc_updates(repo_root)
    except Exception as exc:
        print(f"specky commit-doc: failed, commit is unaffected ({type(exc).__name__}: {exc})")


def install_git_hook() -> Path:
    repo_root = _repo_root()
    hook_path = repo_root / ".git" / "hooks" / "post-commit"
    if hook_path.exists() and HOOK_MARKER not in hook_path.read_text():
        raise RuntimeError(
            f"{hook_path} already exists and wasn't installed by specky — not overwriting it"
        )
    hook_path.write_text(POST_COMMIT_HOOK)
    hook_path.chmod(0o755)
    return hook_path
