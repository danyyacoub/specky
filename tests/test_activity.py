"""The home page's activity brief, over real histories: what counts as one change, whose it is,
and where its words come from.

Each test builds its history the way specky's hook leaves one: a work commit, then a follow-up
doc-sync commit carrying its history doc. That second commit is part of what's under test — it
must never show up as work.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from specky import activity, commit_doc, paths
from specky.activity import ActivityConfig, collect
from specky.html_render import render_site
from specky.indexer import run_index

ALICE = ("Alice Martin", "alice@example.com")
BOB = ("Bob", "bob@example.com")
CAROL = ("Carol", "carol@example.com")
LONG_AGO = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()


def git(repo: Path, *args: str, who=ALICE, date: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0],
        "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": who[0],
        "GIT_COMMITTER_EMAIL": who[1],
    }
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


def work(
    repo: Path,
    message: str,
    who=ALICE,
    *,
    headline: str | None = None,
    impact: str = "feature",
    features: tuple[str, ...] = (),
    legacy: str | None = None,
    date: str | None = None,
) -> str:
    """One commit, and — given a `headline` or `legacy` prose — the hook's doc commit after it."""
    (repo / f"file-{uuid.uuid4().hex[:8]}.txt").write_text(message)  # unique: branches never conflict
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, who=who, date=date)
    sha = git(repo, "rev-parse", "HEAD").strip()
    if headline is not None or legacy is not None:
        doc = (
            commit_doc.MicroDoc(what=legacy)
            if legacy is not None
            else commit_doc.MicroDoc(
                headline=headline, impact=impact, what="What.", features=list(features)
            )
        )
        commit = commit_doc.Commit(sha, f"{who[0]} <{who[1]}>", "2026-01-01", message, "")
        commit_doc.write_history_file(repo, commit, doc)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", commit_doc._AUTO_COMMIT_MARKER, who=who, date=date)
    return sha


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on `main` whose only commit is well outside the window."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / ".gitignore").write_text(".specky/\n")
    work(path, "initial commit", date=LONG_AGO)
    paths.reset_cache()
    return path


def brief(repo: Path, **cfg) -> activity.Activity:
    return collect(repo, ActivityConfig(**cfg))


def person(found: activity.Activity, name: str) -> activity.Person:
    return next(p for p in found.people if p.name == name)


def names(found: activity.Activity) -> set[str]:
    return {p.name for p in found.people}


# --- what counts as one change ------------------------------------------------------------


def _merged_pull_request(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feat/refunds")
    work(repo, "wip", ALICE, headline="Refunds can be partial", features=("specs/billing/refunds.md",))
    work(repo, "address review", BOB, headline="Refund emails name the order", impact="improvement")
    work(repo, "tidy", ALICE, headline="Refund helpers share one module", impact="internal")
    git(repo, "checkout", "-q", "main")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge pull request #12 from octo/feat/refunds\n\nPartial refunds",
        "feat/refunds",
        who=CAROL,
    )


def test_a_merged_branch_is_one_change_told_by_its_commits_docs(repo):
    _merged_pull_request(repo)

    found = brief(repo)

    [change] = person(found, "Alice Martin").shipped
    assert person(found, "Bob").shipped == [change]
    assert [line.text for line in change.lines] == [
        "Refunds can be partial",
        "Refund emails name the order",
        "Refund helpers share one module",
    ]
    assert change.label == "Partial refunds"
    assert change.commits == 3, "the doc-sync commits are not work"
    assert change.people == ["Alice Martin", "Bob"]
    assert change.features[0] == "specs/billing/refunds.md"
    # Carol merged it; the people are the ones who wrote it.
    assert "Carol" not in names(found)


def test_a_direct_commit_is_a_change_of_its_own(repo):
    work(repo, "Cap refunds", headline="Refunds are capped at the order total", impact="fix")

    [change] = person(brief(repo), "Alice Martin").shipped
    assert [(line.text, line.impact) for line in change.lines] == [
        ("Refunds are capped at the order total", "fix")
    ]
    assert change.label == "" and change.commits == 1


def test_a_squash_merge_keeps_its_pr_number(repo):
    work(repo, "Cap refunds (#34)", headline="Refunds are capped")

    [change] = person(brief(repo), "Alice Martin").shipped
    assert change.label == "#34"


def test_speckys_doc_commits_never_show_as_work(repo):
    work(repo, "Cap refunds", headline="Refunds are capped")

    found = brief(repo)
    texts = [line.text for p in found.people for c in p.shipped for line in c.lines]
    assert texts == ["Refunds are capped"]


# --- where the words come from ------------------------------------------------------------


def test_a_commit_without_a_doc_is_shown_by_its_subject_and_counted(repo):
    work(repo, "Cap refunds")

    found = brief(repo)
    [line] = person(found, "Alice Martin").shipped[0].lines
    assert (line.text, line.history_path) == ("Cap refunds", "")
    assert found.undocumented == 1


def test_the_working_tree_doc_wins_so_the_brief_matches_the_page_it_links_to(repo):
    """A doc rewritten but not yet committed (a `--refresh-history` run) is what the viewer renders
    as the commit's page, so it is what the brief says too."""
    sha = work(repo, "Cap refunds", legacy="Refunds are capped. Old words.")
    commit = commit_doc.Commit(sha, "Alice Martin <alice@example.com>", "2026-01-01", "Cap refunds", "")
    commit_doc.write_history_file(repo, commit, commit_doc.MicroDoc(headline="New words", what="w"))

    [line] = person(brief(repo), "Alice Martin").shipped[0].lines
    assert line.text == "New words"


def test_a_legacy_doc_gives_its_first_sentence(repo):
    work(repo, "Cap refunds", legacy="Refunds are capped. They used to be unlimited.")

    [line] = person(brief(repo), "Alice Martin").shipped[0].lines
    assert line.text == "Refunds are capped."
    assert line.history_path.startswith("specs/history/")


# --- people -------------------------------------------------------------------------------


def test_agents_and_bots_are_not_people(repo):
    work(repo, "Cap refunds\n\nCo-authored-by: Claude Opus 5 <noreply@anthropic.com>")
    work(repo, "Bump requests", ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"))
    work(
        repo,
        "Add export\n\nCo-authored-by: Bob <bob@example.com>",
        ("Copilot", "198982749+Copilot@users.noreply.github.com"),
    )

    found = brief(repo)
    assert names(found) == {"Alice Martin", "Bob"}
    assert found.automated == 1


def test_ignore_authors_extends_the_built_in_list(repo):
    work(repo, "Nightly build", ("CI Runner", "ci@build.example.com"))

    assert brief(repo, ignore_authors=("*@build.example.com*",)).people == []


def test_a_branch_no_human_wrote_goes_to_whoever_merged_it(repo):
    git(repo, "checkout", "-q", "-b", "deps")
    work(repo, "Bump requests", ("renovate[bot]", "bot@renovateapp.com"))
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'deps'", "deps", who=CAROL)

    assert names(brief(repo)) == {"Carol"}


def test_one_name_under_two_addresses_is_one_person(repo):
    work(repo, "At work", ("Alice Martin", "alice@work.example.com"))
    work(repo, "At home", ("Alice Martin", "alice@home.example.com"))

    found = brief(repo)
    assert names(found) == {"Alice Martin"}
    assert len(person(found, "Alice Martin").shipped) == 2


# --- in progress --------------------------------------------------------------------------


def test_an_unmerged_branch_is_in_progress_in_the_words_of_its_own_docs(repo):
    git(repo, "checkout", "-q", "-b", "feat/export")
    sha = work(repo, "export", BOB, headline="Orders can be exported as CSV")
    git(repo, "checkout", "-q", "main")
    # The doc was only ever committed on the branch: the checkout can't see it.
    assert commit_doc.history_doc_for(paths.history_dir(repo), sha) is None

    bob = person(brief(repo), "Bob")
    assert bob.shipped == []
    [branch] = bob.in_progress
    assert branch.ref == "feat/export"
    assert [line.text for line in branch.lines] == ["Orders can be exported as CSV"]


def test_remote_branches_are_named_without_the_long_lived_ones(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "checkout", "-q", "-b", "feat/export")
    work(repo, "export", BOB, headline="Orders can be exported as CSV")
    git(repo, "checkout", "-q", "main")
    git(repo, "push", "-q", "origin", "main", "feat/export")
    git(repo, "remote", "set-head", "origin", "main")
    git(repo, "fetch", "-q", "origin")

    found = brief(repo)
    assert found.branch == "origin/main"
    [branch] = person(found, "Bob").in_progress
    assert branch.ref == "origin/feat/export"


# --- the window and the mainline ----------------------------------------------------------


def test_the_window_is_by_landing_not_by_authoring(repo):
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    work(repo, "old direct commit", date=month_ago)
    git(repo, "checkout", "-q", "-b", "slow", "HEAD~1")
    work(repo, "written a month ago", BOB, date=month_ago)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'slow'", "slow")

    found = brief(repo)
    texts = [line.text for p in found.people for c in p.shipped for line in c.lines]
    assert texts == ["written a month ago"]
    assert names(found) == {"Bob"}


def test_a_configured_mainline_is_walked(repo):
    git(repo, "checkout", "-q", "-b", "dev")
    work(repo, "on dev", headline="Dev work")
    git(repo, "checkout", "-q", "main")

    assert brief(repo).people == []
    found = brief(repo, branch="dev")
    assert found.branch == "dev"
    assert [line.text for line in person(found, "Alice Martin").shipped[0].lines] == ["Dev work"]


def test_a_configured_mainline_that_doesnt_exist_is_an_error_not_another_branch(repo):
    with pytest.raises(ValueError, match="nope"):
        brief(repo, branch="nope")


def test_disabled_means_nothing_to_render(repo):
    assert brief(repo, enabled=False) is None


def test_a_shallow_clone_says_so_rather_than_guessing(repo, tmp_path):
    work(repo, "second")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True
    )
    assert brief(shallow).shallow


# --- parsing ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Merge pull request #7 from octo/feat/refund-limits", "Limit refunds", ("Limit refunds", "7")),
        ("Merge pull request #7 from octo/feat/refund-limits", "", ("refund limits", "7")),
        (
            "Merge branch 'feat/x' into 'main'",
            "Export orders\n\nSee merge request group/app!42",
            ("Export orders", "42"),
        ),
        ("Merge branch 'hotfix-login'", "", ("login", "")),
        ("Merged in feature/sso (pull request #9)", "", ("sso", "9")),
    ],
)
def test_merge_labels(subject, body, expected):
    commit = activity._Commit("s", ["a", "b"], ALICE, datetime.now(timezone.utc), [], subject, "", body)
    assert activity._merge_label(commit) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:octo/app.git", "https://github.com/octo/app/pull/5"),
        ("https://github.com/octo/app", "https://github.com/octo/app/pull/5"),
        ("ssh://git@gitlab.example.com:2222/grp/sub/app.git", "https://gitlab.example.com/grp/sub/app/-/merge_requests/5"),
    ],
)
def test_pr_links(repo, url, expected):
    git(repo, "remote", "add", "origin", url)
    assert activity._pr_link(repo)("5") == expected


def test_no_pr_link_for_a_host_it_doesnt_know(repo, tmp_path):
    git(repo, "remote", "add", "origin", str(tmp_path / "elsewhere.git"))
    assert activity._pr_link(repo) is None


# --- the home page ------------------------------------------------------------------------


def test_the_home_page_shows_people_not_agents(repo):
    _merged_pull_request(repo)
    work(repo, "Cap refunds\n\nCo-authored-by: Claude Opus 5 <noreply@anthropic.com>", headline="Refunds are capped", impact="fix")
    run_index(repo)

    home = (render_site(repo).parent / "index.html").read_text()

    assert home.count('<details class="person">') == 2  # Alice and Bob
    assert "Refunds can be partial" in home
    assert "Partial refunds" in home
    assert "+1 internal" in home  # the internal line is counted, not listed
    assert '<span class="impact impact-fix">fix</span>' in home
    assert "Claude" not in home and "anthropic" not in home
    assert "Carol" not in home
