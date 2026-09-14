"""`specky pr-comment` — the markdown summary of a range's doc changes.

Everything here is git-only: no index, no provider, and nothing is posted anywhere. The tests are
mostly about what a reviewer sees, plus one section on the bounds — the range is whatever someone
asks for, and a comment body that grows with it is a comment GitHub refuses to accept.
"""

from __future__ import annotations

import pytest

from specky import cli, prcomment
from specky.prcomment import (
    BODY_MAX_CHARS,
    DETAIL_LIMIT,
    DOC_LIST_LIMIT,
    comment_markdown,
    run_pr_comment,
)

from conftest import git

DOC = "specs/billing/refund-flow.md"


def _doc(summary: str, extra: str = "") -> str:
    return (
        "# Billing — Refund Flow\n\n"
        f"## What It Does\n{summary}\n\n"
        f"## How It Works\n1. **Ask** the ledger.{extra}\n"
    )


def _commit(repo, message: str, files: dict[str, str], remove: tuple[str, ...] = ()) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for rel in remove:
        (repo / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _body(repo, **kwargs) -> str:
    return comment_markdown(run_pr_comment(repo, **kwargs))


# --- what a reviewer sees ---------------------------------------------------------------


def test_a_new_doc_is_listed_with_its_purpose_and_its_summary(tmp_repo):
    _commit(
        tmp_repo,
        "document refunds",
        {
            DOC: _doc("A customer gets their money back."),
            "specs/MODULES.md": "## Billing\n\n| Doc | Purpose |\n|---|---|\n"
            "| [billing/refund-flow.md](billing/refund-flow.md) | Issue refunds |\n",
        },
    )
    body = _body(tmp_repo, base="HEAD~1")

    assert "### Added" in body
    assert "**`billing/refund-flow.md`** — Issue refunds" in body
    assert "> A customer gets their money back." in body
    # MODULES.md changed in the same commit, but its diff is a table row, not a statement about
    # the product — so it's named once and never detailed.
    assert "_Index docs updated: `MODULES.md`._" in body
    assert body.count("MODULES.md") == 1


def test_a_changed_summary_is_shown_as_a_diff(tmp_repo):
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are manual.")})
    _commit(tmp_repo, "automate it", {DOC: _doc("Refunds are automatic.")})
    body = _body(tmp_repo, base="HEAD~1")

    assert "### Updated" in body
    assert "```diff" in body
    assert "-Refunds are manual." in body
    assert "+Refunds are automatic." in body


def test_a_doc_edited_outside_its_summary_says_so(tmp_repo):
    """Otherwise a reviewer can't tell "the summary didn't move" from "specky didn't look" — and a
    regeneration rewrites How It Works and the tables far more often than What It Does."""
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    _commit(tmp_repo, "more detail", {DOC: _doc("Refunds are automatic.", extra="\n2. **Pay**.")})
    body = _body(tmp_repo, base="HEAD~1")

    assert "```diff" not in body
    assert "Summary unchanged" in body


def test_a_removed_doc_is_reported(tmp_repo):
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    _commit(tmp_repo, "drop it", {}, remove=(DOC,))
    body = _body(tmp_repo, base="HEAD~1")

    assert "### Removed" in body
    assert "**`billing/refund-flow.md`**" in body


def test_a_renamed_doc_names_where_it_came_from(tmp_repo):
    """A rename is how a doc moves domain, and its summary diff has to be read from the old path or
    the whole section reads as newly added."""
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    moved = "specs/payments/refund-flow.md"
    _commit(tmp_repo, "move to payments", {moved: _doc("Refunds are instant.")}, remove=(DOC,))
    body = _body(tmp_repo, base="HEAD~1")

    assert "**`payments/refund-flow.md`**" in body
    assert "(was `billing/refund-flow.md`)" in body
    assert "-Refunds are automatic." in body and "+Refunds are instant." in body


def test_per_commit_docs_are_counted_not_listed(tmp_repo):
    """A 300-commit branch has ~300 `specs/history/` docs. Listing them would be the whole comment,
    and none of them is a statement about what the product does."""
    history = {f"specs/history/{i:08x}.md": f"# Commit {i:08x}\n" for i in range(40)}
    _commit(tmp_repo, "docs: sync specky docs [skip specky]", {DOC: _doc("Refunds."), **history})
    body = _body(tmp_repo, base="HEAD~1")

    assert "40 per-commit doc(s) under `specs/history/` not listed." in body
    assert "history/00000000.md" not in body


def test_a_range_with_no_doc_changes_says_exactly_that(tmp_repo):
    _commit(tmp_repo, "just code", {"src/app.py": "x = 1\n"})
    body = _body(tmp_repo, base="HEAD~1")

    assert body == "**Docs:** no changes under `specs/` since `HEAD~1`.\n"


def test_the_target_branchs_own_doc_changes_are_not_credited_to_the_branch(tmp_repo):
    """`git diff base...HEAD`, three dots: the comment describes what this branch did, not what
    landed on the branch it's aimed at after the fork."""
    main = git(tmp_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(tmp_repo, "checkout", "-q", "-b", "feature")
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    git(tmp_repo, "checkout", "-q", main)
    _commit(tmp_repo, "document invoices", {"specs/billing/invoices.md": _doc("Invoices go out.")})
    git(tmp_repo, "checkout", "-q", "feature")

    body = _body(tmp_repo, base=main)
    assert "refund-flow.md" in body
    assert "invoices.md" not in body


# --- refused drafts --------------------------------------------------------------------
# A doc the hook wanted to change and wouldn't is the one thing about this range a reviewer can't
# see anywhere else: the draft is under .specky/, which is gitignored, so it isn't in the diff.


def _stage_draft(repo, rel: str = "billing/refund-flow.md") -> None:
    path = repo / prcomment.PENDING_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a draft the hook refused to write\n")


def test_a_refused_draft_is_reported_alongside_the_docs_that_did_change(tmp_repo):
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    _stage_draft(tmp_repo, "billing/invoices.md")

    body = _body(tmp_repo, base="HEAD~1")
    assert "1 doc update(s) were generated and refused" in body
    assert "`billing/invoices.md`" in body
    assert "specky doctor" in body  # where to go next


def test_a_refused_draft_is_reported_even_when_the_range_changed_no_docs(tmp_repo):
    """The worst case for silence: nothing under `specs/` moved *because* the write was refused."""
    _commit(tmp_repo, "just code", {"src/app.py": "x = 1\n"})
    _stage_draft(tmp_repo)

    body = _body(tmp_repo, base="HEAD~1")
    assert "no changes under `specs/`" in body
    assert "generated and refused" in body


def test_no_drafts_means_no_note(tmp_repo):
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    report = run_pr_comment(tmp_repo, base="HEAD~1")

    assert report.pending_docs == ()
    assert "refused" not in comment_markdown(report)


# --- bounds ----------------------------------------------------------------------------
# specky is installed into other people's repos, so the range is not this repo's range. GitHub
# refuses a comment body over 65,536 characters; these pin that the output can't get there.


@pytest.fixture
def many_docs(tmp_repo):
    """One commit adding far more docs than a comment can detail, each with a long summary."""
    docs = {
        f"specs/d{i:03d}/topic.md": _doc(f"Doc {i} does something. " + "Filler prose. " * 60)
        for i in range(DOC_LIST_LIMIT + 25)
    }
    _commit(tmp_repo, "document everything", docs)
    return tmp_repo


def test_a_long_list_of_docs_is_cut_off_with_a_count(many_docs):
    body = _body(many_docs, base="HEAD~1")

    assert "…and 25 more." in body
    assert "d029/topic.md" in body  # the last one inside the limit
    assert "d030/topic.md" not in body


def test_only_a_bounded_number_of_docs_are_read_from_git(many_docs, monkeypatch):
    """Each detailed doc costs a `git show`. Without a bound this is one git process per changed
    doc, which is the cost model this whole module exists to avoid."""
    real = prcomment.gitlog.run
    shows = []

    def counting(repo_root, args, stdin=None):
        if args[0] == "show":
            shows.append(args)
        return real(repo_root, args, stdin)

    monkeypatch.setattr(prcomment.gitlog, "run", counting)
    run_pr_comment(many_docs, base="HEAD~1")

    assert len(shows) == DETAIL_LIMIT


def test_the_body_stays_postable_however_much_changed(many_docs):
    body = _body(many_docs, base="HEAD~1")

    assert len(body) <= BODY_MAX_CHARS
    assert "Summaries shown for the first" in body


def test_a_body_trimmed_mid_diff_still_closes_its_fence(monkeypatch, tmp_repo):
    """An unterminated ``` swallows the trim note — and everything after it — into a code block."""
    monkeypatch.setattr(prcomment, "BODY_MAX_CHARS", 220)
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are manual. " * 20)})
    _commit(tmp_repo, "reword", {DOC: _doc("Refunds are automatic. " * 20)})

    body = _body(tmp_repo, base="HEAD~1")
    assert "Trimmed:" in body
    assert body.count("```") % 2 == 0


# --- the command -----------------------------------------------------------------------


def test_the_command_prints_the_markdown_and_nothing_else(tmp_repo, monkeypatch, capsys):
    """It's meant to be piped into `gh pr comment --body-file -`, so a `specky pr-comment:` prefix
    or a trailing blank line would end up in the posted comment."""
    _commit(tmp_repo, "document refunds", {DOC: _doc("Refunds are automatic.")})
    monkeypatch.chdir(tmp_repo)
    monkeypatch.setattr("sys.argv", ["specky", "pr-comment", "--base", "HEAD~1"])
    cli.main()

    out = capsys.readouterr().out
    assert out.startswith("**Docs:** 1 added since `HEAD~1`.")
    assert out == comment_markdown(run_pr_comment(tmp_repo, base="HEAD~1"))
