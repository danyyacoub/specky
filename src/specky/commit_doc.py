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
import re
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from specky import frontmatter, paths
from specky.ai_provider import (
    HANDOFF_MARKER,
    ConfigError,
    Provider,
    load_provider_from_toml,
    read_ai_config,
    skill_handoff,
    supports_batch,
)
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

# Which of `HOOKS` each `install-git-hook --on` mode installs. `commit` is every one, and a doc
# commit follows each commit. `merge` is for a team that wants one doc commit per merged branch
# rather than one per commit: `post-merge` alone, because `post-rewrite` fires on every amend and
# would bring the per-commit doc commits straight back — the price is that a rebase orphans a
# history doc it would otherwise have renamed, which `specky sync` or the CI job reconciles.
# `none` leaves documenting to CI.
HOOK_MODES: dict[str, tuple[str, ...]] = {
    "commit": tuple(HOOKS),
    "merge": ("post-merge",),
    "none": (),
}

# The mode is recorded in the repo's local git config, so `specky doctor` can tell a hook left out
# on purpose from one that's missing.
HOOK_MODE_CONFIG = "specky.hooks"

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


# What a commit did to the product, as the micro-doc reports it. `internal` is everything a user
# can't observe — the home page's activity brief folds those away, so a product owner reading it
# sees behaviour first.
IMPACTS = ("feature", "improvement", "fix", "internal")

# The micro-doc instruction, identical for every commit, so it rides as the cacheable prefix.
#
# Structured rather than one paragraph because three readers take different parts of it: the
# activity brief lists headlines and folds `internal` away, the Spec Assistant retrieves the
# what/why prose when asked why something changed, and a feature page lists the commits that
# `features:` ties to it. `why` may be empty on purpose — a motivation the commit doesn't state is
# one the model would have to invent.
MICRO_DOC_PREFIX = (
    "You write the history entry for one git commit. Two readers use it: a product owner scanning "
    "what changed recently, and an assistant answering what the product does and why it changed.\n\n"
    "Reply with only a JSON object:\n"
    '{"headline": "...", "impact": "...", "what_changed": "...", "why": "..."}\n\n'
    "- headline: at most 80 characters, present tense, what the product now does differently, in "
    "its users' terms. No file, function or class names.\n"
    "- impact: exactly one of feature (a new capability), improvement (existing behaviour "
    "changed), fix (wrong behaviour corrected), internal (no behaviour a user can observe: "
    "refactoring, tests, tooling, build, dependencies, documentation).\n"
    "- what_changed: 1-3 sentences on behaviour before and after. Name the commands, flags, "
    "settings and screens a user would recognise; leave implementation detail out.\n"
    "- why: 1-2 sentences on the motivation, as the commit message or diff states it. An empty "
    "string when neither says."
)


def micro_doc_prompt(commit: Commit) -> tuple[str, str]:
    """`(cacheable prefix, this commit's half)`, shared by the serial and batched paths."""
    return (
        MICRO_DOC_PREFIX,
        f"Commit message:\n{commit.message}\n\n"
        f"Diff (may be truncated):\n{commit.diff[:DIFF_TRUNCATE_CHARS]}",
    )


def generate_micro_doc(commit: Commit, provider: Provider) -> str:
    """The model's reply, unparsed: every path that fetches one (serial, prefetched, batched)
    hands the same text to `parse_micro_doc`, so the parse lives in one place."""
    prefix, prompt = micro_doc_prompt(commit)
    return provider.generate(prompt, prefix=prefix, task="summary").strip()


@dataclass
class MicroDoc:
    """What a history doc says about its commit.

    An empty `headline` is the legacy shape — one paragraph under `# Commit <sha8>`, all of it in
    `what` — and is what `specky sync --refresh-history` looks for. A reply that isn't the JSON
    asked for lands in that shape too, rather than having a headline guessed for it: the doc stays
    readable, and a refresh will pick it up again.
    """

    headline: str = ""
    impact: str = ""
    what: str = ""
    why: str = ""
    features: list[str] = field(default_factory=list)

    def text(self) -> str:
        """The prose, as one searchable block — what the index and the Spec Assistant read."""
        return "\n\n".join(part for part in (self.headline, self.what, self.why) if part)


def parse_micro_doc(reply: str) -> MicroDoc:
    from specky.generator import leading_json_object, strip_code_fence  # generator imports us

    answer = leading_json_object(strip_code_fence(reply))
    if answer is None:
        return MicroDoc(what=reply.strip())
    impact = str(answer.get("impact", "")).strip().lower()
    return MicroDoc(
        # One line, whatever came back: it becomes the doc's H1, and a newline in it would end the
        # heading and leave the rest as an orphaned paragraph.
        headline=" ".join(str(answer.get("headline", "")).split()),
        impact=impact if impact in IMPACTS else "",
        what=str(answer.get("what_changed", "")).strip(),
        why=str(answer.get("why", "")).strip(),
    )


def _recorded_sha(path: Path) -> str | None:
    """The full sha a history doc says it documents, or None for one written before that was
    recorded (the filename's 8-hex prefix is all those carry)."""
    recorded = frontmatter.parse(path.read_text())[0].get("sha")
    return recorded if isinstance(recorded, str) else None


def history_names(sha: str) -> tuple[str, str]:
    """The two filenames a commit's history doc can have, in the order they're tried: `<sha8>.md`,
    and `<sha12>.md` for the commit that found its eight-digit name already taken."""
    return f"{sha[:8]}.md", f"{sha[:12]}.md"


def history_doc_for(history_dir: Path, sha: str) -> Path | None:
    """The doc that documents `sha`, or None if this commit still needs one.

    Two names are possible, because 8 hex digits is not a unique key on a large repo: the usual
    `<sha8>.md`, and the `<sha12>.md` written when some other commit got there first. A doc with
    no `sha:` is taken at its filename, which is the best that can be said for one written before
    the full sha was recorded.
    """
    for name in history_names(sha):
        path = history_dir / name
        if path.exists() and _recorded_sha(path) in (None, sha):
            return path
    return None


WHAT_CHANGED_HEADING = "## What changed"
WHY_HEADING = "## Why"

# The legacy H1, which named the commit rather than saying anything about it. `read_history` reads
# it as "no headline", which is what marks a doc as one `--refresh-history` should rewrite.
_LEGACY_TITLE = re.compile(r"^Commit [0-9a-f]{7,40}$")


def write_history_file(repo_root: Path, commit: Commit, doc: MicroDoc) -> Path:
    history_dir = paths.history_dir(repo_root)
    history_dir.mkdir(parents=True, exist_ok=True)

    short, longer = history_names(commit.sha)
    path = history_dir / short
    if path.exists() and _recorded_sha(path) not in (None, commit.sha):
        path = history_dir / longer  # that name is another commit's

    # The full sha is what `history_doc_for` matches on; the body keeps showing the short one,
    # which is what a reader wants to see and copy. `impact` and `features` sit in the frontmatter
    # because they are read by code, not people — the brief, the indexer, a feature page's list.
    meta: dict[str, str | list[str]] = {"sha": commit.sha}
    if doc.impact:
        meta["impact"] = doc.impact
    if doc.features:
        meta["features"] = doc.features
    bullets = (
        f"- **Date:** {commit.date}\n"
        f"- **Author:** {commit.author}\n"
        f"- **Message:** {commit.message.splitlines()[0] if commit.message else ''}\n\n"
    )
    if doc.headline:
        sections = [f"{WHAT_CHANGED_HEADING}\n\n{doc.what}\n"] if doc.what else []
        if doc.why:
            sections.append(f"{WHY_HEADING}\n\n{doc.why}\n")
        body = f"# {doc.headline}\n\n{bullets}" + "\n".join(sections)
    else:
        body = f"# Commit {commit.sha[:8]}\n\n{bullets}{doc.what}\n"
    path.write_text(frontmatter.render(meta, body))
    return path


def read_history(text: str) -> tuple[str | None, MicroDoc] | None:
    """`(the sha it records, what it says)` for a history doc — `write_history_file` in reverse.

    Reads both shapes. A legacy doc (`# Commit <sha8>`, one paragraph) comes back with no headline
    and its paragraph as `what`; callers wanting a one-liner take its first sentence. Read back
    rather than regenerated wherever a doc already exists, so a rename costs no provider call and a
    hand-edited summary survives it. None when there's nothing after the metadata block — a shape
    this didn't write, which a caller should leave alone rather than guess at.
    """
    meta, body = frontmatter.parse(text)
    headline = ""
    prose: list[str] = []
    for line in body.splitlines():
        if not prose and line.startswith("# "):
            title = line[2:].strip()
            headline = "" if _LEGACY_TITLE.match(title) else title
        elif not prose and (line.startswith("- **") or not line.strip()):
            continue
        else:
            prose.append(line)
    rest = "\n".join(prose).strip()

    what, why = rest, ""
    if rest.startswith(WHAT_CHANGED_HEADING) or rest.startswith(WHY_HEADING):
        what, _, why = rest.partition(WHY_HEADING)
        what = what.removeprefix(WHAT_CHANGED_HEADING)
    what, why = what.strip(), why.strip()
    if not (headline or what):
        return None

    impact = meta.get("impact", "")
    features = meta.get("features", [])
    sha = meta.get("sha")
    return (
        sha if isinstance(sha, str) else None,
        MicroDoc(
            headline=headline,
            impact=impact if impact in IMPACTS else "",
            what=what,
            why=why,
            features=list(features) if isinstance(features, list) else [],
        ),
    )


def brief(doc: MicroDoc, limit: int = 200) -> str:
    """One line for a list of changes: the headline, or a legacy doc's first sentence.

    The sentence split is deliberately plain (". " then a capital) — a legacy summary is prose the
    model wrote about code, and an `e.g.` or a `v1.2` mid-sentence must not end it early.
    """
    text = doc.headline or " ".join(doc.what.split())
    if not doc.headline:
        match = re.search(r"[.!?](?=\s+[A-Z])", text)
        if match:
            text = text[: match.end()]
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


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
    legacy: bool = False,
) -> list[tuple[str, str]]:
    """(sha, subject) for every commit still needing a doc, oldest first.

    This is specky's source of truth for "what is undocumented", and both callers are the same
    pass over it: `sync()` unbounded, and a hook fire bounded by `depth`/`HOOK_CATCHUP_MAX`.

    Skipped: commits that already have a history doc (see `history_doc_for`), specky's own
    doc-sync commits — `main()` refuses to document those when the hook fires, and a backfill has
    no business paying to document them either — and merge commits. A clean merge's
    `git show` is an empty combined diff, so its micro-doc was a paid call that said "merged a
    branch"; what the branch did is already in the docs of the commits it brought in, which is
    where the activity brief reads it from.

    `legacy` inverts the doc test for `sync --refresh-history`: only commits whose history doc is
    in the pre-headline shape (see `MicroDoc`), which are the ones a refresh rewrites.

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
    args = ["git", "log", "--reverse", "--no-merges", "--format=%H%x1f%s"]
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
        if subject.startswith(_AUTO_COMMIT_MARKER):
            continue
        doc = history_doc_for(history_dir, sha)
        wanted = _is_legacy(doc) if legacy else doc is None
        if wanted:
            pending.append((sha, subject))
    return pending[:limit] if limit else pending


def handoff_line(todo: Sequence[tuple[str, str]]) -> str:
    """What `main()` prints instead of documenting, when the session agent will do it.

    Starts with `HANDOFF_MARKER`, which the Claude Code hook looks for. Every other host sees the
    line in the output of the `git commit` its agent ran, which is the same place it lands.
    """
    shas = ", ".join(sha[:7] for sha, _ in todo)
    return (
        f"{HANDOFF_MARKER}: {len(todo)} ({shas}) — run the document-commits skill to write "
        "their history docs in this session"
    )


def record_commit(
    repo_root: Path, sha: str, reply: str, feature: str | None = None
) -> Path:
    """Write the history doc for `sha` from a micro-doc reply someone else wrote.

    The `document-commits` skill's half of `_sync_one`: the session agent writes the reply and any
    feature doc itself, and this records it exactly as a provider's reply would have been — same
    parse, same file, same index rows.
    """
    commit = _commit_info(sha, with_diff=False)
    doc = parse_micro_doc(reply)
    if feature:
        doc.features = [feature]
    path = write_history_file(repo_root, commit, doc)
    record_micro_doc(repo_root, commit, doc.text())
    if feature:
        record_commit_link(repo_root, commit.sha, feature)
    return path


def _is_legacy(doc: Path | None) -> bool:
    """Whether a history doc is in the pre-headline shape — False for no doc at all."""
    if doc is None:
        return False
    parsed = read_history(doc.read_text())
    return parsed is not None and not parsed[1].headline


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

    doc = parse_micro_doc(summary if summary is not None else generate_micro_doc(commit, provider))

    # Classified before the history doc is written, so the doc can say which feature it belongs to:
    # `features:` is committed, where the `commit_links` row below lives in a gitignored database a
    # fresh clone doesn't have. A classification that fails still leaves the history doc behind, as
    # it always has — the commit is documented, just not linked.
    try:
        result = sync_feature_doc(repo_root, commit, provider, existing)
    except Exception:
        write_history_file(repo_root, commit, doc)
        record_micro_doc(repo_root, commit, doc.text())
        raise
    if result:
        doc.features = [str(result.path.relative_to(repo_root))]
    history_path = write_history_file(repo_root, commit, doc)
    record_micro_doc(repo_root, commit, doc.text())
    print(f"{label}: wrote {history_path}")
    written = [history_path]

    # A doc can be linked without being written — see generator.DocSync. `written` is what gets
    # committed, so a refused or frozen doc stays out of it while the link, which answers "which
    # doc covers this commit", is recorded either way.
    if result:
        print(f"{label}: {result.note}")
        if result.written:
            written.append(result.path)
        record_commit_link(repo_root, commit.sha, doc.features[0])
    return written


def _refresh_one(repo_root: Path, commit: Commit, reply: str, label: str) -> Path:
    """Rewrite one legacy history doc from a fresh micro-doc reply, keeping what it links to.

    The link comes from the doc if it has one, else from this machine's `commit_links` rows — the
    only record of a legacy doc's feature, and absent on a clone that didn't write it, in which case
    the refreshed doc is simply unlinked, as it was.
    """
    doc = parse_micro_doc(reply)
    old = history_doc_for(paths.history_dir(repo_root), commit.sha)
    old_doc = read_history(old.read_text()) if old else None
    doc.features = old_doc[1].features if old_doc else []
    if not doc.features:
        conn = connect(repo_root)
        try:
            doc.features = [
                path
                for (path,) in conn.execute(
                    "SELECT path FROM commit_links WHERE sha = ? ORDER BY path", (commit.sha,)
                )
            ]
        finally:
            conn.close()
    path = write_history_file(repo_root, commit, doc)
    record_micro_doc(repo_root, commit, doc.text())
    print(f"{label}: refreshed {path}")
    return path


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


def _confirm(count: int, assume_yes: bool, estimate: str) -> None:
    if assume_yes or count < SYNC_CONFIRM_THRESHOLD:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{count} commits ({estimate}) is over the {SYNC_CONFIRM_THRESHOLD}-commit "
            "confirmation threshold and stdin isn't a terminal — re-run with --yes, or narrow it "
            "with --since/--limit"
        )
    answer = input(f"specky sync: {count} commits, {estimate}. Continue? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise RuntimeError("cancelled")


def sync(
    since: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    all_branches: bool = False,
    batch: bool = False,
    refresh_history: bool = False,
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

    `refresh_history` works on documented commits instead: it rewrites each history doc still in
    the legacy one-paragraph shape into the structured one (see `MicroDoc`), over the same range
    flags. One micro-doc call per doc and nothing else — the feature docs are not reclassified, and
    the doc keeps the `features:` link it already has.
    """
    repo_root = _repo_root()
    depth = None if (since or limit or all_branches) else SYNC_DEFAULT_DEPTH
    pending = pending_commits(
        repo_root,
        since=since,
        limit=limit,
        all_branches=all_branches,
        depth=depth,
        legacy=refresh_history,
    )
    total = len(pending)
    if not pending:
        print("specky sync: already up to date")
        return []

    verb = "refresh" if refresh_history else "document"
    estimate = f"~{total} AI calls" if refresh_history else _call_estimate(total)
    if dry_run:
        print(f"specky sync: {total} commits to {verb}, {estimate}")
        for i, (sha, subject) in enumerate(pending, 1):
            print(f"  [{i}/{total}] {sha[:8]} {subject}")
        return []

    _confirm(total, assume_yes, estimate)
    provider = load_provider_from_toml(repo_root / "specky.toml", "sync")  # let ConfigError surface

    try:
        with exclusive(repo_root):
            written = _write_docs(
                repo_root, pending, provider, label_prefix="", batch=batch, refresh=refresh_history
            )
    except LockBusy as exc:
        print(f"specky sync: {exc}")
        return []
    print(f"specky sync: wrote {len(written)} files across {total} commits")

    # Said after the walk rather than instead of it. A repo with no feature docs is not a broken
    # state to be fixed before anything else can happen — the history trail above is written either
    # way, and reference docs are added one feature at a time, when somebody wants one.
    from specky.generator import ExistingDocs  # lazy: generator imports this module

    if not ExistingDocs.load(repo_root).purposes:
        print(
            "specky sync: no feature/workflow docs yet — write one with "
            '`specky document "<feature>"`'
        )
    return written


def _write_docs(
    repo_root: Path,
    pending: list[tuple[str, str]],
    provider: Provider,
    label_prefix: str = "",
    batch: bool = False,
    refresh: bool = False,
) -> list[Path]:
    """Work through a pending list in commit order, batching the micro-doc calls.

    Shared by `sync()` and the hook's catch-up so the two can't drift: same batching, same
    `ExistingDocs` snapshot, same per-commit error containment. Caller holds the lock. `refresh`
    rewrites existing history docs (`_refresh_one`) instead of documenting new commits.
    """
    from specky.generator import ExistingDocs

    total = len(pending)
    # One walk of the docs tree for the whole run; sync_feature_doc folds each doc it writes back
    # into it, so commit 400 is told about the doc commit 3 created. A refresh classifies nothing,
    # so it has no use for one.
    existing = None if refresh else ExistingDocs.load(repo_root)

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
                reply = batched.get(commit.sha) or summaries[commit.sha].result()
                if refresh:
                    written.append(_refresh_one(repo_root, commit, reply, label))
                else:
                    written += _sync_one(
                        repo_root, commit, provider, existing, label=label, summary=reply
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
    paths.state_dir(repo_root)
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


def _contents(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _commit_doc_updates(
    repo_root: Path, written: list[Path], removed: Sequence[Path] = ()
) -> None:
    """Commit exactly the docs this run wrote, as a separate commit.

    Only `written` and `removed` are staged and committed; MODULES.md is in `written` only when this
    run changed it. A bare `git add <docs root>` would sweep up a human's half-finished doc edit —
    somebody who is mid-sentence in a feature doc when a commit lands would find their draft
    committed under specky's name, and reverting the bot's commit would take their work with it.
    The `git commit -- <paths>` pathspec does the same job on the other side: a partial
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
    targets: dict[str, bool] = {}  # rel path -> is a deletion. Deduped, in the order written.
    for path in written:  # a batch often rewrites one feature doc twice
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

    So the doc moves instead: same content — headline, impact, `features:` and prose, via
    `read_history` — new filename, refreshed `sha:` and metadata block (an amend can change the
    message, author and date, all of which git already knows). Free, and it
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
        parsed = read_history(old_doc.read_text())
        if parsed is None:
            continue
        try:
            commit = _commit_info(new_sha, with_diff=False)
        except subprocess.CalledProcessError:
            continue  # the rewrite didn't land (an aborted rebase), so the old doc still stands
        old_doc.unlink()  # before writing: on an amend, both shas can want the same <sha8>.md
        new_doc = write_history_file(repo_root, commit, parsed[1])
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


def _hands_off(repo_root: Path) -> bool:
    try:
        return skill_handoff(read_ai_config(repo_root / "specky.toml"))
    except ConfigError:
        return False  # no provider either; the provider load below says so


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

        # `todo`, not `batch`: `_write_docs` takes a keyword argument called `batch` meaning "use
        # the Batch API", and a local of the same name meaning "the commits to document" is one
        # keyword-ification away from a hook that silently starts batching.
        todo: list[tuple[str, str]] = []
        too_old: list[tuple[str, str]] = []
        if not ours:
            pending = pending_commits(repo_root, depth=HOOK_CATCHUP_DEPTH)
            todo, too_old = pending[:HOOK_CATCHUP_MAX], pending[HOOK_CATCHUP_MAX:]

        if not (todo or written or removed or _read_deferred(repo_root)):
            return  # nothing to document and nothing owed: don't even take the lock

        provider = None
        if todo and _hands_off(repo_root):
            # The agent that made this commit is still there to document it, with the repo in
            # context and its own tools. Launching a headless copy of that same agent would only
            # redo that work worse, so the commits stay pending for its skill instead.
            print(handoff_line(todo))
            todo = []
        if todo:
            try:
                provider = load_provider_from_toml(repo_root / "specky.toml", "commit-doc")
            except ConfigError as exc:
                print(f"specky commit-doc: skipping ({exc})")

        try:
            with exclusive(repo_root):
                modules = paths.modules_index(repo_root)
                modules_before = _contents(modules)
                if provider is not None:
                    written += _write_docs(
                        repo_root, todo, provider, label_prefix="specky commit-doc "
                    )
                # `update_modules_index` edits MODULES.md as a side effect of writing a doc, so it
                # never appears in `written` — and it's a file people edit by hand too. Staging it
                # on every fire committed whatever edit a developer had pending in it under
                # specky's marker, so it goes in only when this fire actually changed it.
                if _contents(modules) != modules_before:
                    written.append(modules)
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


def hook_mode(repo_root: Path) -> str:
    """The `--on` mode `install-git-hook` last recorded, `commit` when none was (or it's unknown)."""
    mode = subprocess.run(
        ["git", "config", "--get", HOOK_MODE_CONFIG], cwd=repo_root, capture_output=True, text=True
    ).stdout.strip()
    return mode if mode in HOOK_MODES else "commit"


def install_git_hook(on: str = "commit") -> list[Path]:
    """Install the hooks `on` asks for (see `HOOK_MODES`), all of them calling `specky commit-doc`.

    Returns the hooks written. Switching to a mode with fewer hooks removes specky's own hooks
    outside it — see `set_hook_mode` for the removed ones too.
    """
    return set_hook_mode(on)[0]


def set_hook_mode(on: str) -> tuple[list[Path], list[Path]]:
    """Install the hooks `on` asks for and remove specky's hooks it doesn't; `(written, removed)`.

    Three hooks because one isn't enough: `post-commit` is invoked by `git commit` only, so
    without `post-merge` and `post-rewrite` a `git pull` or a rebase produces no fire at all.
    They share one entry point — each fire reconciles the backlog — so which one fired barely
    matters; installing all three only makes the reconciliation happen sooner.

    A pre-existing hook specky didn't write is never overwritten, and one foreign hook stops the
    whole install rather than leaving half the set in place: a partial install is the state that's
    hardest to reason about later. A hook specky didn't write is never removed either: a mode that
    leaves it out only takes back what specky put there.
    """
    if on not in HOOK_MODES:
        raise ValueError(f"unknown hook mode {on!r} (expected {'/'.join(HOOK_MODES)})")
    wanted = HOOK_MODES[on]
    repo_root = _repo_root()
    target = hooks_dir(repo_root)
    target.mkdir(parents=True, exist_ok=True)

    def ours(path: Path) -> bool:
        return path.is_file() and HOOK_MARKER in path.read_text()

    foreign = [path for name in wanted if (path := target / name).exists() and not ours(path)]
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
    for name in wanted:
        path = target / name
        path.write_text(_HOOK_BODY.format(args=HOOKS[name], specky=specky))
        path.chmod(0o755)
        written.append(path)
    removed = []
    for name in HOOKS:
        if name not in wanted and ours(path := target / name):
            path.unlink()
            removed.append(path)
    subprocess.run(["git", "config", HOOK_MODE_CONFIG, on], cwd=repo_root, check=True)
    return written, removed
