"""Per-commit micro-doc generation, called by git's post-commit/post-merge/post-rewrite hooks.

These are deliberately real git hooks rather than agent-specific ones (Claude Code
PostToolUse, opencode tool.execute.after, Kiro agent hooks) — they must fire on every
commit regardless of which agent (or no agent) made it. Each commit gets a real
markdown file under specs/history/, plus a mirrored row in the micro_docs sqlite
table for Phase 3's indexer to pick up.

**A hook fire reconciles a backlog; it does not document "the commit that just happened".**
That distinction is the whole reliability story, because git gives no hook that fires for every
new commit. `post-commit` is invoked by `git commit` and nothing else (see githooks(5)): a merge
commit, a rebase, a cherry-pick, a revert, a `git am`, a squash-merge performed in the forge's
web UI, and a commit by anyone who never ran `install-git-hook` all produce no fire at all. So
`main()` asks `pending_commits()` — the diff between git history and what's already in
specs/history/ — and works through the tail of it. A hook then only has to fire *eventually*,
which is a property git does give us, and `specky sync` is the same pass without the bound.

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

import os
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from specky import frontmatter, paths
from specky.ai_provider import ConfigError, Provider, load_provider_from_toml, supports_batch
from specky.db import connect, repo_root as _repo_root
from specky.lock import LockBusy, exclusive

if TYPE_CHECKING:  # generator imports from this module, so it can only be imported lazily here
    from specky.generator import ExistingDocs

# The hook scripts, one per git event specky listens to. `{specky}` is filled in with the absolute
# path of the `specky` that installed them, and it is the point of the whole template: git hooks
# run from a plain, often login-less shell, and a GUI client (IntelliJ, Fork, Tower, VS Code's git
# integration) frequently starts with a PATH that has no `~/.local/bin` in it. The old script was
# `command -v specky … || true`, which in that shell is a silent no-op — the single most common
# reason a repo has the hook installed and no docs to show for it. So: try PATH first (it survives
# `uv tool upgrade` moving the binary), then fall back to the recorded absolute path.
_HOOK_BODY = """#!/bin/sh
# Installed by `specky install-git-hook`. Reconciles the history-doc backlog for this repo.
if command -v specky >/dev/null 2>&1; then
    specky commit-doc{args} || true
elif [ -x "{specky}" ]; then
    "{specky}" commit-doc{args} || true
fi
exit 0
"""

# post-rewrite is handed `<old-sha> <new-sha>` pairs on stdin, which lets an amend or a rebase
# *rename* the history doc it already paid for instead of orphaning it (see `apply_rewrites`).
HOOKS = {
    "post-commit": "",
    "post-merge": "",
    "post-rewrite": " --rewritten",
}

HOOK_MARKER = "specky commit-doc"

# Set this in the environment and every hook fire returns immediately. Uninstalling the hook is the
# better answer when you control the hooks directory — but you often don't: a repo that commits its
# own hooks and points `core.hooksPath` at them (the husky-shaped setup) hands specky's hooks to
# every clone, including ones with no provider key and no business spending on docs. A cloud coding
# agent's VM is the case this was added for: its commits belong in the pull request it opens, and a
# doc commit nobody asked for landing in that branch is a surprise, not a feature.
DISABLE_HOOK_ENV = "SPECKY_DISABLE_HOOK"

# Values that read as "off" rather than "set". Without them `SPECKY_DISABLE_HOOK=0` — the obvious
# way to write "no" — would disable the hook.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def hook_disabled() -> bool:
    return os.environ.get(DISABLE_HOOK_ENV, "").strip().lower() not in _FALSEY

# Prefix for the follow-up commit `main()` makes for whatever it writes under specs/. Checked
# at the top of `main()` so that commit's own post-commit firing recognizes itself and returns
# immediately instead of generating a doc *for* the doc-sync commit and recursing forever.
_AUTO_COMMIT_MARKER = "docs: sync specky docs [skip specky]"

# Where a fire that couldn't commit (git midway through a rebase or a cherry-pick) leaves the list of
# paths for the next fire to commit. Under `.specky/`, so it's gitignored and per-checkout.
DEFERRED_LEDGER = "deferred-docs"

# Diffs are truncated to this many characters before going into a prompt — long enough for
# context, short enough to keep prompt cost/latency predictable regardless of commit size.
DIFF_TRUNCATE_CHARS = 8000

# How many commits' micro-doc summaries `sync()` asks for at once, and the commit count above
# which it stops to confirm first. Adopting specky on an existing repo means one `specky sync`
# over its whole history — hundreds of billable calls — so that has to be a deliberate yes.
SYNC_CONCURRENCY = 4
SYNC_CONFIRM_THRESHOLD = 25

# How far back a bare `specky sync` (no --since/--limit/--all-branches) looks. Without this, the
# first run on an existing repo silently walks its entire history — the whole-history backfill
# the confirmation above exists to make deliberate becomes the *default*, not something asked
# for. Any of the three range flags means the caller already has a range in mind, so it overrides
# this rather than stacking with it.
SYNC_DEFAULT_DEPTH = 10

# How much of the backlog a *hook* fire will look at and act on. Both bounds matter, for different
# reasons:
#
# - DEPTH bounds the `git log` walk, so a hook on a repo with 200k commits costs the same as one on
#   a repo with 20. Anything older than this is `specky sync`'s job, which is what `specky doctor`
#   already tells people (its own probe uses the same window).
# - MAX bounds the *spending*. A `git pull` that fast-forwards 300 undocumented commits fires
#   post-merge once, and that one fire must not turn into 600 provider calls inside a git hook.
#   The rest stays pending and is reported.
HOOK_CATCHUP_DEPTH = 20
HOOK_CATCHUP_MAX = 5

# Sequencer state files. While any of these exists, git is midway through a multi-commit operation
# and `_commit_doc_updates` must not run a `git commit`: doing so during a rebase or cherry-pick
# writes a commit into the middle of someone else's replay, which at best confuses the sequencer and
# at worst has to be untangled by hand. Docs are still written — they just wait for the next fire (or
# a `specky sync`) to be committed.
_SEQUENCER_PATHS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "sequencer",
)


@dataclass
class Commit:
    sha: str
    author: str
    date: str
    message: str
    diff: str


def _commit_info(rev: str = "HEAD", with_diff: bool = True) -> Commit:
    """Metadata for one revision, plus its diff unless the caller has no use for it.

    `with_diff=False` exists for `apply_rewrites`, which re-renders a doc's metadata block for a
    new sha and never prompts: a rebase of 50 commits shouldn't pay for 50 `git show` calls to
    produce diffs nothing reads.
    """
    sha, author, date, message = subprocess.run(
        ["git", "log", "-1", "--format=%H%x1f%an <%ae>%x1f%aI%x1f%B", rev],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    ).stdout.split("\x1f", 3)
    # `errors="replace"` and not the default strict: a diff carries file bytes verbatim, and git
    # only omits content it detects as binary. A file with no early NUL but non-UTF-8 bytes — a
    # PDF whose header is `%\x93\x8c\x8b\x9e`, a source file saved in CP1252 — is emitted as text
    # and used to abort the whole run on a UnicodeDecodeError. The diff is truncated to
    # DIFF_TRUNCATE_CHARS for the prompt anyway, so a U+FFFD in it costs nothing.
    diff = (
        subprocess.run(
            ["git", "show", "--format=", sha],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        ).stdout
        if with_diff
        else ""
    )
    return Commit(sha=sha, author=author, date=date, message=message.strip(), diff=diff)


# The micro-doc instruction, identical for every commit, so it rides as the cacheable prefix.
MICRO_DOC_PREFIX = (
    "Summarize what changed and why, in one short paragraph, for a commit history reader."
)


def micro_doc_prompt(commit: Commit) -> tuple[str, str]:
    """`(cacheable prefix, this commit's half)`, shared by the serial and batched paths."""
    return (
        MICRO_DOC_PREFIX,
        f"Commit message:\n{commit.message}\n\n"
        f"Diff (may be truncated):\n{commit.diff[:DIFF_TRUNCATE_CHARS]}",
    )


def generate_micro_doc(commit: Commit, provider: Provider) -> str:
    prefix, prompt = micro_doc_prompt(commit)
    return provider.generate(prompt, prefix=prefix, task="summary").strip()


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
    history_dir = paths.history_dir(repo_root)
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
    depth: int | None = None,
) -> list[tuple[str, str]]:
    """(sha, subject) for every commit still needing a doc, oldest first.

    This is specky's source of truth for "what is undocumented", and both callers are the same
    pass over it: `sync()` unbounded, and a hook fire bounded by `depth`/`HOOK_CATCHUP_MAX`.

    Skipped: commits that already have a history doc (see `history_doc_for`), and specky's own
    doc-sync commits — `main()` refuses to document those when the hook fires, and a backfill has
    no business paying to document them either.

    `since` accepts either form a user is likely to reach for: a revision (`v1.2.0`, `HEAD~50`,
    a sha) becomes `<since>..HEAD`, and anything else is handed to git as `--since=<date>`
    ("2 weeks ago", "2026-01-01"). `limit` caps the result *after* filtering, so `--limit 5`
    means five commits actually processed, not five inspected.

    `depth` caps it the other way round — `git log -n <depth>`, so only the newest `depth` commits
    are *inspected* at all. That's what keeps a hook fire's cost independent of how long the repo's
    history is. It's spelled this way rather than as `since="HEAD~20"` on purpose: on a repo with
    fewer commits than that, `HEAD~20` isn't a revision, so it would fall through `_is_revision`
    and be handed to git as a *date*, which silently matches everything.

    `all_branches` walks every ref instead of just `HEAD`, so work that only exists on a side
    branch gets documented too. Opt-in, because it multiplies the commit count — and therefore
    the number of billable calls — that `_confirm` exists to make deliberate.
    """
    history_dir = paths.history_dir(repo_root)

    # `git log` rather than `rev-list` for the subject line, which the progress and --dry-run
    # output both want; the revision walking is identical.
    args = ["git", "log", "--reverse", "--format=%H%x1f%s"]
    if depth:
        # git applies `-n` before `--reverse`, so this is the newest `depth` commits, reversed.
        args += [f"-n{depth}"]
    tip = ["--all"] if all_branches else ["HEAD"]
    if since and _is_revision(repo_root, since):
        # `--all --not <rev>` is the multi-ref spelling of `<rev>..HEAD`.
        args += [*tip, "--not", since] if all_branches else [f"{since}..HEAD"]
    elif since:
        args += [f"--since={since}", *tip]
    else:
        args += tip

    log = subprocess.run(
        args, cwd=repo_root, capture_output=True, text=True, errors="replace", check=True
    ).stdout
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
    feature_docs: bool = True,
) -> list[Path]:
    """History log entry (changelog trail) + feature/workflow reference doc, if this commit
    affects one. The specs/<domain>/<topic>.md docs are the reference; specs/history/ is just
    the supplementary per-commit trail alongside them.

    `existing` lets a multi-commit caller (`sync()`) reuse one walk of specs/ across every
    commit; the single-commit hook path leaves it out. `label` prefixes this commit's output —
    `sync()` passes a `[12/431] abc1234` progress marker. `summary` is the micro-doc when the
    caller already fetched it (see `_prefetch_summaries`).

    `feature_docs=False` writes the history entry and stops. It's for the commits in a run that has
    just bootstrapped: those docs were written from the working tree at HEAD, which already
    *contains* every one of these commits, so asking a model to update them from the same commits'
    diffs is redundant at best. At worst it's destructive — and observably so: on a real run every
    such update came back as a whole-body rewrite that `lost_content` had to refuse, leaving drafts
    in `.specky/pending/` for docs that were correct to begin with."""
    from specky.generator import sync_feature_doc

    if summary is None:
        summary = generate_micro_doc(commit, provider)
    history_path = write_history_file(repo_root, commit, summary)
    record_micro_doc(repo_root, commit, summary)
    print(f"{label}: wrote {history_path}")
    written = [history_path]
    if not feature_docs:
        return written

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


def _batch_summaries(commits: list[Commit], provider: Provider) -> dict[str, str]:
    """Every pending commit's micro-doc in one batched request, keyed by sha.

    The micro-doc is the one call in the commit path that batches cleanly: it depends on nothing
    but its own commit. Classification deliberately cannot — it is fed the running `ExistingDocs`
    snapshot, and answering a whole backlog against one frozen snapshot is how a run ends up with
    three docs about one subject (see `_prefetch_summaries`).

    Whole-run rather than per-batch-of-four, because the Batches API's win is per *request*, not
    per call, and one round trip for 400 commits is the point. Anything that goes wrong returns
    `{}` and the ordinary concurrent path runs instead.
    """
    if not supports_batch(provider, "summary"):
        print("specky sync: --batch ignored, this provider has no batch API")
        return {}
    prompts = {c.sha: micro_doc_prompt(c) for c in commits}
    print(f"specky sync: sending {len(prompts)} summary requests as one batch")
    try:
        answers = provider.generate_batch(prompts, task="summary")
    except Exception as exc:
        print(f"specky sync: batch failed ({exc}) — falling back to concurrent calls")
        return {}
    return {sha: text.strip() for sha, text in answers.items()}


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
    bootstrap: bool = True,
    batch: bool = False,
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
    confirmation that a large backfill otherwise stops for. Bare `sync()` — none of those three —
    only looks at the newest `SYNC_DEFAULT_DEPTH` commits; pass any one of them to see further
    back (e.g. `--since <first commit>` for the whole history on a fresh adopt).
    """
    # Imported here rather than at module scope: `bootstrap` imports `generator`, which imports this
    # module, so a top-level import would be a cycle. Same reason `_sync_one` imports
    # `sync_feature_doc` inside its body.
    from specky.bootstrap import bootstrap as run_bootstrap, needs_bootstrap

    repo_root = _repo_root()
    cold = bootstrap and needs_bootstrap(repo_root)
    depth = None if (since or limit or all_branches) else SYNC_DEFAULT_DEPTH
    pending = pending_commits(
        repo_root, since=since, limit=limit, all_branches=all_branches, depth=depth
    )
    total = len(pending)
    if not (pending or cold):
        print("specky sync: already up to date")
        return []

    if dry_run:
        if cold:
            print("specky sync: no feature docs yet — would bootstrap from the code first")
        print(f"specky sync: {total} commits to document, {_call_estimate(total)}")
        for i, (sha, subject) in enumerate(pending, 1):
            print(f"  [{i}/{total}] {sha[:8]} {subject}")
        return []

    _confirm(total, assume_yes)
    provider = load_provider_from_toml(repo_root / "specky.toml", "sync")  # let ConfigError surface

    bootstrapped: list[Path] = []
    try:
        with exclusive(repo_root):
            # Bootstrap first, so the commit walk below classifies into the docs it writes instead
            # of inventing parallel ones: `_document` loads its own `ExistingDocs` snapshot, and on
            # a cold repo that list is empty — which is exactly the case the classification prompt
            # warns about when it calls a second doc on one subject a defect.
            if cold:
                print("specky sync: no feature docs yet — writing them from the code first")
                try:
                    bootstrapped = run_bootstrap(
                        repo_root, provider, assume_yes=assume_yes, batch=batch, label="specky sync"
                    )
                except Exception as exc:
                    # Declining the bootstrap confirmation, or a discovery call that failed, must
                    # not cost the commit walk — that's the part the user actually asked for, and
                    # it works on a repo with no docs exactly as it did before this existed.
                    print(f"specky sync: skipping bootstrap ({exc})")
            # `feature_docs=False` when bootstrap just ran: see `_sync_one`. These commits are
            # already in the docs it wrote, so this pass only owes them a history entry — which also
            # takes the cold-start path from ~2.5 provider calls per commit down to one.
            written = _document(
                repo_root,
                pending,
                provider,
                label_prefix="",
                feature_docs=not bootstrapped,
                batch=batch,
            )
    except LockBusy as exc:
        print(f"specky sync: {exc}")
        return []
    # Counted separately, because they are not the same claim: bootstrap's files came from the code
    # and `written`'s from the commit walk, and "12 files across 5 commits" would be a lie about
    # where ten of them came from.
    if bootstrapped:
        print(f"specky sync: wrote {len(bootstrapped)} files from the code")
    print(f"specky sync: wrote {len(written)} files across {total} commits")
    return bootstrapped + written


def _document(
    repo_root: Path,
    pending: list[tuple[str, str]],
    provider: Provider,
    label_prefix: str = "",
    feature_docs: bool = True,
    batch: bool = False,
) -> list[Path]:
    """Work through a pending list in commit order, batching the micro-doc calls.

    Shared by `sync()` and the hook's catch-up so the two can't drift: same batching, same
    `ExistingDocs` snapshot, same per-commit error containment. Caller holds the lock.
    """
    from specky.generator import ExistingDocs

    total = len(pending)
    # One walk of the docs tree for the whole run; sync_feature_doc folds each doc it writes back
    # into it, so commit 400 is told about the doc commit 3 created.
    existing = ExistingDocs.load(repo_root)

    # With `--batch`, every commit's summary is fetched up front in one request; the per-chunk
    # `_prefetch_summaries` below then has nothing left to ask for and the loop is unchanged.
    batched: dict[str, str] = {}
    if batch:
        batched = _batch_summaries([_commit_info(sha) for sha, _ in pending], provider)

    written: list[Path] = []
    for start in range(0, total, SYNC_CONCURRENCY):
        chunk = [_commit_info(sha) for sha, _ in pending[start : start + SYNC_CONCURRENCY]]
        summaries = _prefetch_summaries([c for c in chunk if c.sha not in batched], provider)
        for offset, commit in enumerate(chunk):
            label = f"{label_prefix}[{start + offset + 1}/{total}] {commit.sha[:8]}"
            try:
                written += _sync_one(
                    repo_root,
                    commit,
                    provider,
                    existing,
                    label=label,
                    summary=batched.get(commit.sha) or summaries[commit.sha].result(),
                    feature_docs=feature_docs,
                )
            except Exception as exc:
                print(f"{label}: skipped ({exc})")
    return written


def _git_path(repo_root: Path, name: str) -> Path:
    """Where git would keep `name` for this repo — `git rev-parse --git-path`.

    Asked of git rather than assembled as `repo_root / ".git" / name`, because `.git` is a *file*
    in a linked worktree and several of these live in the common dir shared by all worktrees.
    """
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", name],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return repo_root / out  # relative to the worktree root, absolute paths absorb the join


def sequencer_in_progress(repo_root: Path) -> str | None:
    """The name of the multi-commit git operation currently underway, or None.

    See `_SEQUENCER_PATHS`: while one of these exists, `git commit` is either refused outright or
    lands in the middle of someone else's replay.
    """
    for name in _SEQUENCER_PATHS:
        if _git_path(repo_root, name).exists():
            return name
    return None


def _read_deferred(repo_root: Path) -> dict[str, bool]:
    """Paths an earlier fire wrote but couldn't commit; `True` means the path must end up *gone*.

    Which half a path was matters: a doc that a rewrite deleted has to be committed as a deletion
    even if a `git checkout` or a merge has since put the file back (see `_commit_doc_updates`).
    """
    ledger = repo_root / ".specky" / DEFERRED_LEDGER
    if not ledger.exists():
        return {}
    entries: dict[str, bool] = {}
    for line in ledger.read_text().splitlines():
        if rel := line.strip().removeprefix("-"):
            entries[rel] = line.strip().startswith("-")
    return entries


def _write_deferred(repo_root: Path, targets: Mapping[str, bool]) -> None:
    """Record paths for the next fire to commit, or clear the record when there are none left."""
    ledger = repo_root / ".specky" / DEFERRED_LEDGER
    if not targets:
        ledger.unlink(missing_ok=True)
        return
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text("".join(f"{'-' if gone else ''}{rel}\n" for rel, gone in targets.items()))


def _stageable(repo_root: Path, rel_paths: list[str]) -> list[str]:
    """The subset git will accept as a pathspec: on disk, or tracked and so stageable as a deletion.

    A path that is neither — an untracked doc a previous fire wrote and `apply_rewrites` has since
    renamed away — would make `git add` fail with `pathspec did not match any files` and take the
    whole commit down with it.
    """
    missing = [rel for rel in rel_paths if not (repo_root / rel).exists()]
    tracked: set[str] = set()
    if missing:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", *missing],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        tracked = {rel for rel in listed.stdout.split("\0") if rel}
    return [rel for rel in rel_paths if (repo_root / rel).exists() or rel in tracked]


def _commit_doc_updates(
    repo_root: Path, written: list[Path], removed: Sequence[Path] = ()
) -> None:
    """Commit exactly the docs this run wrote, as a separate commit.

    Only `written` and `removed` (plus MODULES.md, which update_modules_index edits as a side
    effect) is staged and committed. A bare `git add <docs root>` would sweep up a human's
    half-finished doc edit — somebody who is mid-sentence in a feature doc when a commit lands would
    find their draft committed under specky's name, and reverting the bot's commit would take their
    work with it. The `git commit -- <paths>` pathspec does the same job on the other side: a partial
    commit leaves anything else the user had staged staged. `removed` is the deleted half of a
    rename, which has to be staged explicitly for the same reason — nothing else would notice it.

    Mid-sequencer the commit can't happen (see `_SEQUENCER_PATHS`), so the paths go into a ledger
    under `.specky/` and the next fire commits them. Without that, a `post-rewrite` that fires while
    `rebase-merge` still exists leaves the rename in the working tree forever: the deleted old-sha
    doc comes back with the next `git checkout` or merge, orphaned, and the new-sha doc stays
    untracked, so a clean clone and CI both see that commit as undocumented and pay to redo it.

    Best-effort like the rest of this module: a failure here (another hook rejecting the commit, a
    rebase in progress) is printed, not raised — the commit being documented already succeeded and
    must stay that way. A rejected commit also un-stages what it staged (`_unstage`), or the same
    sweep this function exists to prevent happens through the developer's next commit instead.
    """
    modules = paths.modules_index(repo_root)
    targets: dict[str, bool] = {}  # rel path -> is a deletion. Deduped, in the order written.
    for path in [*written, modules]:  # a batch often rewrites one feature doc twice
        if path.exists() and path.is_relative_to(repo_root):
            targets[path.relative_to(repo_root).as_posix()] = False
    for path in removed:
        if path.is_relative_to(repo_root):
            targets.setdefault(path.relative_to(repo_root).as_posix(), True)
    for rel, gone in _read_deferred(repo_root).items():
        targets.setdefault(rel, gone)

    if (operation := sequencer_in_progress(repo_root)) is not None:
        if targets:
            _write_deferred(repo_root, targets)
            print(
                f"specky commit-doc: {len(targets)} doc file(s) written but left uncommitted — git "
                f"is midway through an operation ({operation}). They're committed by the next fire, "
                "or by `git add` + `git commit` once you're done."
            )
        return

    # A deletion the ledger is still carrying, whose file is back: a checkout or a fast-forward
    # restored the *committed* old-sha doc, which now documents a commit that isn't in the history.
    # It goes again — leaving it would re-commit the orphan the rename existed to remove.
    for rel, gone in targets.items():
        if gone:
            (repo_root / rel).unlink(missing_ok=True)

    staged_paths = _stageable(repo_root, list(targets))
    if not staged_paths:
        return  # nothing written this run (e.g. every commit was skipped by classification)

    # Whatever the human already had staged among these paths, recorded before `git add` overwrites
    # those index entries: it's the one thing `_unstage` must not undo.
    pre_staged = set(_index_differs(repo_root, staged_paths))
    try:
        subprocess.run(["git", "add", "--", *staged_paths], cwd=repo_root, check=True)
        if not _index_differs(repo_root, staged_paths):
            _write_deferred(repo_root, {})  # already committed by someone else; stop carrying them
            return  # the docs were regenerated byte-for-byte identical
        # Cleared *before* the commit: that commit fires this hook again, and a nested fire that
        # found the ledger still full would report the lock it can't take as a problem.
        _write_deferred(repo_root, {})
        subprocess.run(
            ["git", "commit", "-m", _AUTO_COMMIT_MARKER, "--", *staged_paths],
            cwd=repo_root,
            check=True,
        )
        print("specky commit-doc: committed doc updates")
    except subprocess.CalledProcessError as exc:
        _write_deferred(repo_root, targets)
        _unstage(repo_root, [rel for rel in staged_paths if rel not in pre_staged])
        print(f"specky commit-doc: doc updates written but not committed ({exc})")


def _index_differs(repo_root: Path, rel_paths: list[str]) -> list[str]:
    """Which of `rel_paths` are staged, i.e. differ between the index and HEAD."""
    listed = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--", *rel_paths],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return [rel for rel in listed.stdout.split("\0") if rel]


def _unstage(repo_root: Path, rel_paths: list[str]) -> None:
    """Drop these paths from the index, leaving the files on disk.

    Called when the doc commit itself fails, and it's what keeps that failure from costing the
    developer their next commit. `git add` above stages the docs into the *real* index, so a commit
    a pre-commit gate rejects — the pre-commit framework's `end-of-file-fixer` and
    `trailing-whitespace` both match `.md`, so it's a normal thing to hit — would otherwise leave
    them staged, and the developer's next `git commit` sweeps specky's docs into their feature
    commit under their name. The ledger's retry is a fire too late to prevent that: the human
    commits before the next hook fires.

    A path the human had already staged is excluded by the caller: their staged content is gone
    (the doc was regenerated over it) and unstaging would compound that, not undo it.
    """
    if rel_paths:
        subprocess.run(["git", "reset", "-q", "--", *rel_paths], cwd=repo_root, check=False)


def _rewrite_pairs(stdin_text: str) -> list[tuple[str, str]]:
    """`<old-sha> SP <new-sha>` lines, which is post-rewrite's stdin contract (githooks(5)).

    A third field is possible for `git rebase -i`'s squashes and is ignored; a line that isn't two
    shas is skipped rather than raising, because this runs in a hook.
    """
    pairs = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and all(len(p) >= 7 for p in parts[:2]):
            pairs.append((parts[0], parts[1]))
    return pairs


def _existing_summary(path: Path) -> str | None:
    """The prose a history doc ends with, separated from the metadata block above it.

    The doc's shape is a heading, a run of `- **Key:** value` bullets, then the summary. Read back
    rather than regenerated so a rewrite costs no provider call, and so a hand-edited summary
    survives the rename. Returns None if the file doesn't have that shape, in which case the
    caller leaves the doc alone rather than guessing.
    """
    body = frontmatter.parse(path.read_text())[1]
    lines = body.splitlines()
    cut = 0
    for i, line in enumerate(lines):
        if line.startswith(("# ", "- **")) or not line.strip():
            cut = i + 1
        else:
            break
    summary = "\n".join(lines[cut:]).strip()
    return summary or None


def _repoint_rows(repo_root: Path, old_sha: str, new_sha: str) -> None:
    """Move the index rows keyed by the old sha onto the new one.

    `OR REPLACE` on both, because a rebase can map two old commits onto one new sha (an
    interactive squash), and the second move would otherwise collide with the first on the
    primary key. The `documents` table isn't touched: `specky index` rebuilds it from the files.
    """
    conn = connect(repo_root)
    try:
        conn.execute("UPDATE OR REPLACE micro_docs SET sha = ? WHERE sha = ?", (new_sha, old_sha))
        conn.execute("UPDATE OR REPLACE commit_links SET sha = ? WHERE sha = ?", (new_sha, old_sha))
        conn.commit()
    finally:
        conn.close()


def apply_rewrites(repo_root: Path, stdin_text: str) -> list[tuple[Path, Path]]:
    """Follow `git commit --amend` / `git rebase` by *renaming* the history docs they invalidated.

    An amend replaces one commit with another that has a different sha, and a rebase does it for
    every replayed commit. Without this, the doc for the old sha is orphaned — it documents a
    commit that is no longer in the history — and the new sha looks undocumented, so the next fire
    pays a provider call to write what is almost exactly the same paragraph again.

    So the doc moves instead: same summary, new filename, refreshed `sha:` and metadata block (an
    amend can change the message, author and date, all of which git already knows). Free, and it
    keeps `pending_commits` honest, which is what everything else here reads.

    Pairs whose old doc doesn't exist are left for the ordinary catch-up to document.

    Returns `(old path, new path)` per rename. Both halves matter to the caller: the rename has to be
    *committed*, and staging only the new file would leave the old one — a doc for a sha that is no
    longer in the history — to come back with the next checkout.
    """
    history_dir = paths.history_dir(repo_root)
    moved: list[tuple[Path, Path]] = []
    for old_sha, new_sha in _rewrite_pairs(stdin_text):
        if old_sha == new_sha:
            continue
        old_doc = history_doc_for(history_dir, old_sha)
        if old_doc is None:
            continue
        summary = _existing_summary(old_doc)
        if summary is None:
            continue
        try:
            commit = _commit_info(new_sha, with_diff=False)
        except subprocess.CalledProcessError:
            continue  # the rewrite didn't land (an aborted rebase), so the old doc still stands
        old_doc.unlink()  # before writing: on an amend, both shas can want the same <sha8>.md
        new_doc = write_history_file(repo_root, commit, summary)
        _repoint_rows(repo_root, old_sha, commit.sha)
        moved.append((old_doc, new_doc))
        print(f"specky commit-doc: {old_doc.name} -> {new_doc.name} ({old_sha[:8]} was rewritten)")
    return moved


def _head_subject(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main(rewritten: bool = False) -> None:
    """Hook entry point for post-commit, post-merge and post-rewrite alike.

    Works on the *backlog* rather than on HEAD (see the module docstring): whatever git event
    caused this fire, the job is the same — take the tail of `pending_commits()` and close as much
    of it as one fire is allowed to. That's what makes a merge, a rebase or a colleague's
    hookless commit end up documented anyway.

    Never raises. The commit it's documenting has already landed, so every failure mode here — bad
    config, provider down, unparseable response — is a printed line and a clean exit. The installed
    hook's `|| true` is a second belt; this is the actual guarantee.
    """
    if hook_disabled():
        # One line rather than silence: "the hook is installed and no docs appear" is the hardest
        # specky failure to diagnose, and this makes the reason show up in the same output the
        # commit did.
        print(f"specky commit-doc: skipping, {DISABLE_HOOK_ENV} is set")
        return
    try:
        repo_root = _repo_root()
        renames: list[tuple[Path, Path]] = []
        if rewritten:
            # Before the backlog is computed: renaming the docs an amend/rebase invalidated is what
            # keeps their commits *out* of the pending list.
            renames = apply_rewrites(repo_root, sys.stdin.read())
        written = [new for _, new in renames]
        removed = [old for old, _ in renames]

        # Our own doc-sync commit's fire, or a rebase replaying one. Nothing new to document —
        # anything still pending is picked up by the next real commit or by `specky sync` — but a
        # rename or an earlier fire's deferred write still has to be committed, so fall through to
        # `_commit_doc_updates` rather than returning here. That can't recurse: the commit it makes
        # stages exactly these paths, so the fire it triggers finds nothing left to do.
        ours = _head_subject(repo_root).startswith(_AUTO_COMMIT_MARKER)

        # `todo`, not `batch`: `_document` now takes a keyword argument called `batch` meaning
        # "use the Batch API", and a local of the same name meaning "the commits to document" is
        # one keyword-ification away from a hook that silently starts batching.
        todo: list[tuple[str, str]] = []
        too_old: list[tuple[str, str]] = []
        if not ours:
            pending = pending_commits(repo_root, depth=HOOK_CATCHUP_DEPTH)
            todo, too_old = pending[:HOOK_CATCHUP_MAX], pending[HOOK_CATCHUP_MAX:]

        if not (todo or written or removed or _read_deferred(repo_root)):
            return  # nothing to document and nothing owed: don't even take the lock

        provider = None
        if todo:
            try:
                provider = load_provider_from_toml(repo_root / "specky.toml", "commit-doc")
            except ConfigError as exc:
                print(f"specky commit-doc: skipping ({exc})")

        try:
            with exclusive(repo_root):
                if provider is not None:
                    written += _document(
                        repo_root, todo, provider, label_prefix="specky commit-doc "
                    )
                # Inside the lock: the commit below fires this hook again, and the nested fire
                # finding the lock held is what stops two of them interleaving.
                _commit_doc_updates(repo_root, written, removed=removed)
        except LockBusy as exc:
            print(f"specky commit-doc: {exc}")
            return

        if too_old and provider is not None:
            print(
                f"specky commit-doc: {len(too_old)} older commit(s) still undocumented — run "
                "`specky sync` to catch up"
            )
    except Exception as exc:
        print(f"specky commit-doc: failed, commit is unaffected ({type(exc).__name__}: {exc})")


def hooks_dir(repo_root: Path) -> Path:
    """The directory git actually runs hooks from.

    `repo_root/.git/hooks` is wrong twice over: in a linked `git worktree` `.git` is a file, and a
    repo that sets `core.hooksPath` (which `pre-commit`, husky and lefthook all do) has git reading
    hooks from somewhere else entirely — so a hook written to the assumed path is silently never
    run, which is indistinguishable from specky being broken.
    """
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=repo_root, capture_output=True, text=True
    ).stdout.strip()
    if configured:
        # A relative hooksPath is resolved against the worktree root, which is where git runs hooks.
        return repo_root / Path(configured).expanduser()
    return _git_path(repo_root, "hooks")


def install_git_hook() -> list[Path]:
    """Install every hook in `HOOKS`, all of them calling `specky commit-doc`.

    Three hooks because one isn't enough: `post-commit` is invoked by `git commit` only, so
    without `post-merge` and `post-rewrite` a `git pull` or a rebase produces no fire at all.
    They share one entry point — each fire reconciles the backlog — so which one fired barely
    matters; installing all three only makes the reconciliation happen sooner.

    A pre-existing hook specky didn't write is never overwritten, and one foreign hook stops the
    whole install rather than leaving half the set in place: a partial install is the state that's
    hardest to reason about later.
    """
    repo_root = _repo_root()
    target = hooks_dir(repo_root)
    target.mkdir(parents=True, exist_ok=True)

    foreign = [
        path
        for name in HOOKS
        if (path := target / name).exists() and HOOK_MARKER not in path.read_text()
    ]
    if foreign:
        raise RuntimeError(
            f"{', '.join(str(p) for p in foreign)} already exist(s) and wasn't installed by specky "
            "— not overwriting it. Add `specky commit-doc || true` to it by hand (and "
            "`specky commit-doc --rewritten` to post-rewrite)"
        )

    # Recorded now, while we're running as the `specky` the user just invoked: the hook needs an
    # absolute path to fall back on when it runs from a PATH that lacks it.
    specky = shutil.which("specky") or sys.argv[0]
    written = []
    for name, args in HOOKS.items():
        path = target / name
        path.write_text(_HOOK_BODY.format(args=args, specky=specky))
        path.chmod(0o755)
        written.append(path)
    return written
