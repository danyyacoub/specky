"""`specky adopt` — importing a repo's pre-existing markdown into the docs tree.

The behaviour worth pinning down isn't the file copy, it's the three ways this command refuses to
guess: a destination that's taken is skipped rather than suffixed, an adopted doc is frozen with
`authored: human` rather than left open to the hook, and nothing is committed. Plus the payoff —
once a doc lives under the docs root, `ExistingDocs` sees it, which is the whole reason adoption
imports rather than merely noting the file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specky import adopt, frontmatter
from conftest import git


def write(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def commit_all(repo: Path, message: str = "add docs") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def documented_repo(tmp_repo: Path) -> Path:
    """A repo that already had documentation before specky arrived."""
    write(tmp_repo, "docs/billing/refunds.md", "# Refund flow\n\nHow refunds work.\n")
    write(tmp_repo, "docs/billing/README.md", "# Billing\n\nThe billing area.\n")
    write(tmp_repo, "docs/billing/api/refunds.md", "# Refund API\n\nEndpoints.\n")
    write(tmp_repo, "ARCHITECTURE.md", "# Architecture\n\nThe shape of it.\n")
    commit_all(tmp_repo)
    return tmp_repo


class TestDiscover:
    def test_finds_documentation_directories_and_root_level_docs(self, documented_repo: Path):
        found = adopt.discover(documented_repo)
        assert found == [
            "ARCHITECTURE.md",
            "docs/billing/README.md",
            "docs/billing/api/refunds.md",
            "docs/billing/refunds.md",
        ]

    def test_leaves_repo_furniture_alone(self, documented_repo: Path):
        write(documented_repo, "CONTRIBUTING.md", "# Contributing\n")
        commit_all(documented_repo, "add contributing")
        assert "README.md" not in adopt.discover(documented_repo)
        assert "CONTRIBUTING.md" not in adopt.discover(documented_repo)

    def test_include_overrides_the_furniture_rule(self, documented_repo: Path):
        assert "README.md" in adopt.discover(documented_repo, include=("README.md",))

    def test_exclude_wins_over_everything(self, documented_repo: Path):
        found = adopt.discover(
            documented_repo, include=("README.md",), exclude=("README.md", "docs/billing/api/*")
        )
        assert found == ["ARCHITECTURE.md", "docs/billing/README.md", "docs/billing/refunds.md"]

    def test_ignores_the_docs_root_itself(self, documented_repo: Path):
        # Otherwise a second run would re-adopt what the first one imported, and every doc would
        # end up one directory deeper each time.
        write(documented_repo, "specs/billing/something.md", "# Something\n")
        commit_all(documented_repo, "add a specky doc")
        assert not any(p.startswith("specs/") for p in adopt.discover(documented_repo))

    def test_untracked_and_ignored_markdown_is_invisible(self, tmp_repo: Path):
        # Discovery asks git, so a vendored dependency's docs and a file nobody has committed yet
        # are both excluded by construction rather than by a denylist per ecosystem.
        write(tmp_repo, ".gitignore", "node_modules/\n")
        write(tmp_repo, "node_modules/dep/docs/guide.md", "# Vendored\n")
        write(tmp_repo, "docs/untracked.md", "# Not committed yet\n")
        git(tmp_repo, "add", ".gitignore")
        git(tmp_repo, "commit", "-q", "-m", "add gitignore")
        assert adopt.discover(tmp_repo) == []


class TestDestination:
    @pytest.mark.parametrize(
        "source, expected",
        [
            ("docs/billing/refunds.md", "specs/billing/refunds.md"),
            # Deeper nesting folds into the topic: a domain is one level by definition, and
            # dropping the middle segment would collide this with docs/billing/refunds.md.
            ("docs/billing/api/refunds.md", "specs/billing/api-refunds.md"),
            ("docs/billing/README.md", "specs/billing/overview.md"),
            ("docs/billing/index.md", "specs/billing/overview.md"),
            ("ARCHITECTURE.md", "specs/architecture/overview.md"),
            ("docs/refunds.md", "specs/docs/refunds.md"),
            ("adr/0007-use-postgres.md", "specs/adr/0007-use-postgres.md"),
            ("docs/Billing Rules.md", "specs/docs/billing-rules.md"),
            ("docs/api/RefundFlow.md", "specs/api/refund-flow.md"),
        ],
    )
    def test_maps_the_old_layout_onto_domain_topic(
        self, tmp_repo: Path, source: str, expected: str
    ):
        assert adopt.destination(tmp_repo, source) == expected

    def test_domain_override_applies_to_every_source(self, tmp_repo: Path):
        assert adopt.destination(tmp_repo, "docs/refunds.md", "Billing") == "specs/billing/refunds.md"

    def test_follows_a_configured_docs_root(self, tmp_repo: Path):
        from specky import paths

        (tmp_repo / "specky.toml").write_text('[docs]\nroot = "documentation"\n')
        paths.reset_cache()
        try:
            assert adopt.destination(tmp_repo, "docs/billing/refunds.md") == (
                "documentation/billing/refunds.md"
            )
        finally:
            paths.reset_cache()


class TestAdopt:
    def test_imports_a_doc_with_provenance_and_the_human_freeze(self, documented_repo: Path):
        adopt.run_adopt(documented_repo, assume_yes=True)

        adopted = documented_repo / "specs/billing/refunds.md"
        meta, body = frontmatter.parse(adopted.read_text())
        assert meta["authored"] == "human"
        assert meta["origin"] == "docs/billing/refunds.md"
        assert "How refunds work." in body
        # Left for `specky tag`, which is the thing that classifies docs.
        assert "type" not in meta and "tags" not in meta

    def test_move_is_a_rename_git_can_follow(self, documented_repo: Path):
        adopt.run_adopt(documented_repo, assume_yes=True)

        assert not (documented_repo / "docs/billing/refunds.md").exists()
        # Staged as a rename, not as a delete plus an unrelated add — which is what keeps
        # `git log --follow` (and specky's own staleness dates) working across the import. The
        # second status column is `M`: the frontmatter is added after the move.
        status = git(documented_repo, "status", "--porcelain")
        assert "RM docs/billing/refunds.md -> specs/billing/refunds.md" in status

    def test_keep_leaves_the_original_in_place(self, documented_repo: Path):
        adopt.run_adopt(documented_repo, mode="keep", assume_yes=True)

        assert (documented_repo / "docs/billing/refunds.md").exists()
        assert (documented_repo / "specs/billing/refunds.md").exists()

    def test_stub_leaves_a_working_relative_link_behind(self, documented_repo: Path):
        adopt.run_adopt(documented_repo, mode="stub", assume_yes=True)

        stub = (documented_repo / "docs/billing/refunds.md").read_text()
        assert "specs/billing/refunds.md" in stub
        link = stub.split("](")[1].split(")")[0]
        assert ((documented_repo / "docs/billing") / link).resolve().exists()

    def test_a_taken_destination_is_reported_and_skipped(self, documented_repo: Path):
        write(documented_repo, "specs/billing/refunds.md", "# Refunds\n\nSpecky's own version.\n")

        report = adopt.run_adopt(documented_repo, assume_yes=True)

        assert ("docs/billing/refunds.md", "specs/billing/refunds.md already exists") in report.skipped
        # Untouched on both sides: no overwrite, and no silent `-2` suffix either.
        assert "Specky's own version." in (documented_repo / "specs/billing/refunds.md").read_text()
        assert (documented_repo / "docs/billing/refunds.md").exists()
        assert "docs/billing/refunds.md" not in [a.source for a in report.adopted]

    def test_two_sources_mapping_to_one_destination_keep_the_first(self, tmp_repo: Path):
        write(tmp_repo, "docs/billing/README.md", "# Billing\n")
        write(tmp_repo, "docs/billing/overview.md", "# Billing overview\n")
        commit_all(tmp_repo)

        report = adopt.run_adopt(tmp_repo, assume_yes=True)

        assert [a.source for a in report.adopted] == ["docs/billing/README.md"]
        assert report.skipped == [
            ("docs/billing/overview.md", "specs/billing/overview.md is already taken by docs/billing/README.md")
        ]

    def test_existing_frontmatter_survives(self, tmp_repo: Path):
        write(
            tmp_repo,
            "docs/billing/refunds.md",
            frontmatter.render({"owner": "payments-team", "authored": "generated"}, "# Refunds\n"),
        )
        commit_all(tmp_repo)

        adopt.run_adopt(tmp_repo, assume_yes=True)

        meta, _ = frontmatter.parse((tmp_repo / "specs/billing/refunds.md").read_text())
        assert meta["owner"] == "payments-team"
        # `authored` was already set, so adoption doesn't overwrite the repo's own answer.
        assert meta["authored"] == "generated"

    def test_dry_run_touches_nothing(self, documented_repo: Path):
        report = adopt.run_adopt(documented_repo, dry_run=True)

        assert len(report.adopted) == 4
        assert report.dry_run
        assert (documented_repo / "docs/billing/refunds.md").exists()
        assert not (documented_repo / "specs/billing").exists()
        assert git(documented_repo, "status", "--porcelain") == ""

    def test_nothing_is_committed(self, documented_repo: Path):
        before = git(documented_repo, "rev-parse", "HEAD")

        adopt.run_adopt(documented_repo, assume_yes=True)

        assert git(documented_repo, "rev-parse", "HEAD") == before
        assert git(documented_repo, "status", "--porcelain") != ""

    def test_adopted_docs_get_a_modules_index_row(self, documented_repo: Path):
        adopt.run_adopt(documented_repo, assume_yes=True)

        modules = (documented_repo / "specs/MODULES.md").read_text()
        assert "## Billing" in modules
        # The purpose column is the doc's own H1 — free, no provider call.
        assert "[billing/refunds.md](billing/refunds.md) | Refund flow |" in modules

    def test_the_import_is_what_stops_sync_writing_a_duplicate(self, documented_repo: Path):
        """The payoff: adoption makes existing docs visible to classification.

        `ExistingDocs.load` reads the docs root and nothing else, so before adoption the prompt's
        "docs that already exist" list is blind to `docs/billing/refunds.md` and the next sync
        invents a twin of it.
        """
        from specky.generator import ExistingDocs

        assert "billing/refunds" not in ExistingDocs.load(documented_repo).docs_block()

        adopt.run_adopt(documented_repo, assume_yes=True)

        assert "billing/refunds — Refund flow" in ExistingDocs.load(documented_repo).docs_block()

    def test_a_repo_with_no_existing_docs_is_a_no_op(self, tmp_repo: Path):
        report = adopt.run_adopt(tmp_repo, assume_yes=True)

        assert report.adopted == [] and report.skipped == []
        assert git(tmp_repo, "status", "--porcelain") == ""

    def test_rejects_an_unknown_mode(self, tmp_repo: Path):
        with pytest.raises(ValueError, match="unknown mode"):
            adopt.run_adopt(tmp_repo, mode="teleport")

    def test_a_large_import_stops_for_confirmation(self, documented_repo: Path, monkeypatch):
        for i in range(adopt.ADOPT_CONFIRM_THRESHOLD):
            write(documented_repo, f"docs/area{i}/topic.md", f"# Topic {i}\n")
        commit_all(documented_repo, "add many docs")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(RuntimeError, match="confirmation threshold"):
            adopt.run_adopt(documented_repo)

        assert not (documented_repo / "specs/area0").exists()

    def test_report_names_the_narrow_sync_as_the_next_step(self, documented_repo: Path):
        lines = adopt.report_lines(adopt.run_adopt(documented_repo, assume_yes=True))

        text = "\n".join(lines)
        assert "docs/billing/refunds.md → specs/billing/refunds.md" in text
        # A full-history sync on an already-documented repo pays to describe commits these docs
        # already cover, so the suggested command is deliberately bounded.
        assert "specky sync --since" in text
