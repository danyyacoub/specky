"""The changelog: one entry per day of user-facing changes, from the history docs, with release
tags as markers — on the site, on the home page, in the in-page search and over MCP."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from specky import changelog, doc_tools, html_render
from specky.indexer import run_index

from conftest import git

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)


def _history(
    repo: Path,
    name: str,
    when: datetime,
    headline: str,
    impact: str = "feature",
    example: bool = False,
    author: str = "Alice Martin <alice@example.com>",
) -> None:
    body = (
        f"# {headline}\n\n- **Date:** {when.isoformat()}\n- **Author:** {author}\n"
        f"- **Message:** {headline}\n\n## What changed\n\n{headline} — details.\n"
    )
    if example:
        body += (
            "\n## Example\n\n**Scenario:** Run `specky check` on a stale doc\n\n"
            "- **Before:** It passed.\n- **After:** It fails, naming the doc.\n"
        )
    directory = repo / "specs" / "history"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ncommits: [{name * 5}]\nimpact: {impact}\n---\n\n{body}"
    )


def test_changes_are_grouped_by_day_newest_first_breaking_first(tmp_repo):
    _history(tmp_repo, "aaaaaaaa", NOW - timedelta(hours=3), "Adds export")
    _history(tmp_repo, "bbbbbbbb", NOW - timedelta(hours=5), "Drops --legacy", impact="breaking")
    _history(tmp_repo, "cccccccc", NOW - timedelta(days=2), "Fixes totals", impact="fix")

    days = changelog.days(tmp_repo, 7, now=NOW)

    assert [d.anchor for d in days] == ["2026-10-08", "2026-10-06"]
    assert [e.headline for e in days[0].entries] == ["Drops --legacy", "Adds export"]
    assert days[0].entries[1].authors == ("Alice Martin",)


def test_a_day_with_only_internal_work_is_left_out_and_so_is_an_old_change(tmp_repo):
    _history(tmp_repo, "aaaaaaaa", NOW - timedelta(days=1), "Bumps deps", impact="internal")
    _history(tmp_repo, "bbbbbbbb", NOW - timedelta(days=30), "Adds export")
    assert changelog.days(tmp_repo, 7, now=NOW) == []
    assert len(changelog.days(tmp_repo, 90, now=NOW)) == 1


def test_a_matching_tag_is_a_marker_on_its_day_and_never_an_entry(tmp_repo):
    _history(tmp_repo, "aaaaaaaa", datetime.now(timezone.utc), "Adds export")
    git(tmp_repo, "tag", "-a", "v1.2.0", "-m", "v1.2.0")
    git(tmp_repo, "tag", "-a", "nightly-42", "-m", "nightly")

    (day,) = changelog.days(tmp_repo, 7)

    assert day.releases == ["v1.2.0"]
    assert [e.headline for e in day.entries] == ["Adds export"]


def test_a_checkout_without_git_has_days_and_no_markers(tmp_path):
    repo = tmp_path / "docs-only"
    (repo / "specs").mkdir(parents=True)
    for i in range(15):
        _history(repo, f"{i:08x}", NOW - timedelta(hours=i), f"Change {i}")
    days = changelog.days(repo, 7, now=NOW)
    assert sum(len(d.entries) for d in days) == 15  # no cap, unlike the brief's docs-only view
    assert all(d.releases == [] for d in days)


def test_the_example_reaches_the_entry(tmp_repo):
    _history(tmp_repo, "aaaaaaaa", NOW, "Check fails on stale docs", example=True)
    (entry,) = changelog.days(tmp_repo, 7, now=NOW)[0].entries
    assert entry.scenario == "Run `specky check` on a stale doc"
    assert (entry.before, entry.after) == ("It passed.", "It fails, naming the doc.")


def test_config_is_read_from_specky_toml(tmp_repo):
    (tmp_repo / "specky.toml").write_text(
        '[changelog]\ndays = 30\nhome_days = 3\ntag_pattern = "release-*"\n'
    )
    cfg = changelog.ChangelogConfig.load(tmp_repo)
    assert (cfg.days, cfg.home_days, cfg.tag_pattern, cfg.enabled) == (30, 3, "release-*", True)


def test_the_site_has_a_changelog_page_a_home_block_and_search_entries(tmp_repo, write_doc):
    write_doc("billing/refunds.md", "# Refunds\n\nRefunds.\n", {"type": "feature"})
    now = datetime.now(timezone.utc)
    _history(tmp_repo, "aaaaaaaa", now, "Drops --legacy", impact="breaking", example=True)
    _history(tmp_repo, "bbbbbbbb", now - timedelta(days=20), "Adds export")
    run_index(tmp_repo)

    site = html_render.render_site(tmp_repo).parent
    page = (site / "changelog.html").read_text()
    home = (site / "index.html").read_text()
    data = (site / "assets" / "site-data.js").read_text()

    today = now.date().isoformat()
    assert f'id="{today}"' in page and "Adds export" in page
    assert '<span class="impact impact-breaking">breaking</span>' in page
    assert '<dl class="example">' in page and "It fails, naming the doc." in page
    # The home shows the last week only, and links to the rest.
    assert '<section class="activity changelog">' in home and 'href="changelog.html"' in home
    assert "Drops --legacy" in home
    changelog_html = home.split('<section class="activity changelog">', 1)[1].split("</section></section>")[0]
    assert "Adds export" not in changelog_html
    index = json.loads(data.split("const SPECKY_INDEX = ", 1)[1].split(";\n", 1)[0])
    hrefs = {e["html_path"] for e in index if e["doc_type"] == "changelog"}
    assert f"changelog.html#{today}" in hrefs


def test_changelog_can_be_turned_off(tmp_repo):
    (tmp_repo / "specky.toml").write_text("[changelog]\nenabled = false\n")
    _history(tmp_repo, "aaaaaaaa", datetime.now(timezone.utc), "Adds export")
    run_index(tmp_repo)
    site = html_render.render_site(tmp_repo).parent
    assert not (site / "changelog.html").exists()
    assert "activity changelog" not in (site / "index.html").read_text()


def test_recent_activity_has_the_days_with_their_examples(tmp_repo):
    now = datetime.now(timezone.utc)
    _history(tmp_repo, "aaaaaaaa", now, "Check fails on stale docs", impact="breaking", example=True)
    _history(tmp_repo, "bbbbbbbb", now - timedelta(days=1), "Bumps deps", impact="internal")

    (day,) = doc_tools.recent_activity(tmp_repo, since="7d")["days"]

    assert day["date"] == now.date().isoformat()
    (change,) = day["changes"]
    assert change["impact"] == "breaking"
    assert change["example"]["after"] == "It fails, naming the doc."
