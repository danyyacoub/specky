"""`owner:` frontmatter — the "who to ask" line, from the doc to the index, viewer and gate.

Nothing generates an owner. It's the one field in specky's frontmatter that only a human writes, so
what matters here is that it survives everything specky does to a doc afterwards — above all the
post-commit hook's regeneration, which runs unattended and would otherwise strip the line within a
day of someone adding it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from specky import catalog, cli, db
from specky.check import report_lines, run_check
from specky.commit_doc import Commit
from specky.generator import sync_feature_doc
from specky.html_render import render_site
from specky.indexer import run_index

from conftest import FakeProvider, git

DOC = "specs/billing/refund-flow.md"


def _owner(repo: Path, path: str = DOC) -> str:
    conn = db.connect(repo)
    try:
        return conn.execute("SELECT owner FROM documents WHERE path = ?", (path,)).fetchone()[0]
    finally:
        conn.close()


# --- the index ------------------------------------------------------------------------------


def test_an_owner_is_indexed_verbatim(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "feature", "owner": "#payments"})
    run_index(tmp_repo)
    assert _owner(tmp_repo) == "#payments"


def test_a_list_of_owners_becomes_one_readable_string(tmp_repo, write_doc):
    """`owner: [ana, bo]` is a reasonable thing to write, and every consumer displays the field
    rather than matching on it, so it's flattened at index time instead of downstream."""
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "feature", "owner": ["ana", "bo"]})
    run_index(tmp_repo)
    assert _owner(tmp_repo) == "ana, bo"


def test_a_doc_without_an_owner_indexes_as_empty(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "feature"})
    run_index(tmp_repo)
    assert _owner(tmp_repo) == ""


# --- `specky features` ----------------------------------------------------------------------


def test_features_lists_who_to_ask(tmp_repo, write_doc, monkeypatch, capsys):
    write_doc(
        "billing/refund-flow.md", "# Refunds\n", {"type": "feature", "owner": "Payments team"}
    )
    run_index(tmp_repo)
    assert catalog.list_features(tmp_repo)[0]["owner"] == "Payments team"

    monkeypatch.chdir(tmp_repo)
    cli._list_docs(argparse.Namespace(command="features"))
    assert "ask: Payments team" in capsys.readouterr().out


def test_features_says_nothing_when_a_doc_has_no_owner(tmp_repo, write_doc, monkeypatch, capsys):
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "feature"})
    run_index(tmp_repo)
    monkeypatch.chdir(tmp_repo)
    cli._list_docs(argparse.Namespace(command="features"))
    assert "ask:" not in capsys.readouterr().out


# --- the viewer -----------------------------------------------------------------------------


def _page(repo: Path, name: str = "billing-refund-flow.html") -> str:
    return (render_site(repo).parent / name).read_text()


def test_the_doc_page_says_who_to_ask(tmp_repo, write_doc):
    write_doc(
        "billing/refund-flow.md", "# Refunds\n", {"type": "feature", "owner": "Payments team"}
    )
    run_index(tmp_repo)
    page = _page(tmp_repo)
    assert "Who to ask: <strong>Payments team</strong>" in page
    assert "#icon-person" in page


def test_the_line_is_absent_rather_than_empty_when_nobody_owns_the_doc(tmp_repo, write_doc):
    write_doc("billing/refund-flow.md", "# Refunds\n", {"type": "feature"})
    run_index(tmp_repo)
    assert "Who to ask" not in _page(tmp_repo)


def test_an_owner_is_escaped_like_any_other_hand_written_field(tmp_repo, write_doc):
    """`owner:` is free text a reader supplies, and it lands in the page body — so a `<` in it has
    to arrive as text rather than as the start of a tag."""
    write_doc(
        "billing/refund-flow.md", "# Refunds\n", {"type": "feature", "owner": "<b>ana</b> & bo"}
    )
    run_index(tmp_repo)
    page = _page(tmp_repo)
    assert "&lt;b&gt;ana&lt;/b&gt; &amp; bo" in page
    assert "<b>ana</b>" not in page


# --- `specky check` -------------------------------------------------------------------------


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _covered(repo: Path, owner: str = "") -> None:
    """Two documented commits pairing `src/app.py` with a classified doc, as the hook would."""
    meta = f"---\ntype: feature\nowner: {owner}\n---\n" if owner else "---\ntype: feature\n---\n"
    for i in range(2):
        sha = _commit(repo, f"refund work {i}", {"src/app.py": f"def refund{i}(): ...\n"})
        _commit(
            repo,
            "docs: sync specky docs [skip specky]",
            {
                f"specs/history/{sha[:8]}.md": f"---\nsha: {sha}\n---\n\n# Commit {sha[:8]}\n",
                DOC: f"{meta}\n# Refunds\n\nRevision {i}.\n",
            },
        )
    run_index(repo)


def test_check_asks_for_an_owner_without_failing_the_build(tmp_repo):
    _covered(tmp_repo)
    _commit(tmp_repo, "more refund work", {"src/app.py": "def refund2(): ...\n"})
    run_index(tmp_repo)

    report = run_check(tmp_repo, base="HEAD~5")
    assert report.unowned == (DOC,)
    assert report.violations == ()  # the doc *is* the point of the range; only the owner is missing
    text = "\n".join(report_lines(report))
    assert "no `owner:`" in text and DOC in text


def test_a_doc_with_an_owner_is_not_mentioned(tmp_repo):
    _covered(tmp_repo, owner="Payments team")
    _commit(tmp_repo, "more refund work", {"src/app.py": "def refund2(): ...\n"})
    run_index(tmp_repo)
    assert run_check(tmp_repo, base="HEAD~5").unowned == ()


def test_history_docs_are_never_asked_to_name_an_owner(tmp_repo):
    """One per commit, machine-written, nobody's to hand-edit — on a repo with 3,000 commits this
    is the difference between one line of advice and three thousand."""
    _covered(tmp_repo)
    report = run_check(tmp_repo, base="HEAD~4")
    assert not [p for p in report.unowned if p.startswith("specs/history/")]


def test_unowned_docs_outside_the_range_are_left_alone(tmp_repo, write_doc):
    """Scoped to the range, like every other piece of advice `check` prints: a repo adopting
    `owner:` shouldn't be told about all of specs/ on every pull request."""
    _covered(tmp_repo, owner="Payments team")
    write_doc("search/indexing.md", "# Indexing\n", {"type": "feature"})
    _commit(tmp_repo, "document indexing", {})  # unowned, and landed before the range starts
    _commit(tmp_repo, "more refund work", {"src/app.py": "def refund2(): ...\n"})
    run_index(tmp_repo)

    assert run_check(tmp_repo, base="HEAD~1").unowned == ()


def test_a_doc_this_range_edited_is_worth_asking_about(tmp_repo, write_doc):
    """The one case that isn't about coverage: someone has the doc open in this very diff, so
    they're who can add the line."""
    write_doc("search/indexing.md", "# Indexing\n", {"type": "feature"})
    _commit(tmp_repo, "document indexing", {})
    run_index(tmp_repo)

    report = run_check(tmp_repo, base="HEAD~1")
    assert report.unowned == ("specs/search/indexing.md",)
    assert report.violations == ()


def test_the_json_report_carries_every_unowned_doc(tmp_repo):
    _covered(tmp_repo)
    assert run_check(tmp_repo, base="HEAD~4").as_dict()["unowned"] == [DOC]


# --- regeneration ---------------------------------------------------------------------------


def _classified(topic: str = "refund-flow") -> FakeProvider:
    return FakeProvider(
        [
            '{"skip": false, "domain": "billing", "topic": "' + topic + '", '
            '"purpose": "p", "type": "feature", "tags": ["refunds"]}',
            "# New body\n",
        ]
    )


def _fake_commit() -> Commit:
    return Commit(
        sha="a" * 40,
        author="Test <t@example.com>",
        date="2026-01-01",
        message="feat: refunds",
        diff="+ refund code",
    )


def test_regenerating_a_doc_keeps_its_hand_written_owner(tmp_repo, write_doc):
    write_doc(
        "billing/refund-flow.md",
        "# Old\n",
        {"type": "feature", "tags": ["old"], "owner": "Payments team"},
    )
    text = sync_feature_doc(tmp_repo, _fake_commit(), _classified()).path.read_text()
    assert "owner: Payments team" in text
    assert "tags: [refunds]" in text  # this run's classification still wins for the fields it owns


def test_regeneration_never_invents_an_owner(tmp_repo):
    text = sync_feature_doc(tmp_repo, _fake_commit(), _classified()).path.read_text()
    assert "owner:" not in text


@pytest.mark.parametrize("owner", ["Payments team", "#payments", "ana@example.com"])
def test_an_owner_round_trips_through_a_regeneration_and_the_index(tmp_repo, write_doc, owner):
    write_doc("billing/refund-flow.md", "# Old\n", {"type": "feature", "owner": owner})
    sync_feature_doc(tmp_repo, _fake_commit(), _classified())
    run_index(tmp_repo)
    assert _owner(tmp_repo) == owner
