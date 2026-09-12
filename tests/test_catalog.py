"""catalog reads the `documents` table, so these go through the real indexer first."""

import pytest

from specky import catalog, db
from specky.indexer import run_index


@pytest.fixture
def indexed_repo(tmp_repo, write_doc):
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n\nIssues refunds.\n",
        {"type": "workflow", "tags": ["refunds", "billing"]},
    )
    write_doc(
        "billing/refund-limits.md",
        "# Billing — Refund Limits\n\nCaps refunds.\n",
        {"type": "feature", "tags": ["refunds"]},
    )
    write_doc(
        "search/indexing.md",
        "# Search — Indexing\n\nIndexes docs.\n",
        {"type": "feature", "tags": ["search"], "related": ["billing/refund-flow"]},
    )
    write_doc("notes.md", "# Notes\n\nUnclassified, so invisible to catalog.\n")
    run_index(tmp_repo)
    return tmp_repo


def test_list_features_and_workflows(indexed_repo):
    features = {d["path"] for d in catalog.list_features(indexed_repo)}
    workflows = {d["path"] for d in catalog.list_workflows(indexed_repo)}
    assert features == {"specs/billing/refund-limits.md", "specs/search/indexing.md"}
    assert workflows == {"specs/billing/refund-flow.md"}


def test_unclassified_docs_are_excluded(indexed_repo):
    paths = {d["path"] for d in catalog.list_features(indexed_repo) + catalog.list_workflows(indexed_repo)}
    assert "specs/notes.md" not in paths


def test_list_tags_groups_docs_by_tag(indexed_repo):
    by_tag = catalog.list_tags(indexed_repo)
    assert list(by_tag) == sorted(by_tag)  # alphabetical
    assert len(by_tag["refunds"]) == 2
    assert [d["path"] for d in by_tag["search"]] == ["specs/search/indexing.md"]


def test_commit_counts_come_from_commit_links(indexed_repo):
    conn = db.connect(indexed_repo)
    conn.execute(
        "INSERT INTO commit_links (sha, path) VALUES (?, ?)",
        ("a" * 40, "specs/billing/refund-flow.md"),
    )
    conn.commit()
    conn.close()
    workflow = catalog.list_workflows(indexed_repo)[0]
    assert workflow["commits"] == 1


def test_build_graph_links_workflows_to_features_sharing_a_tag(indexed_repo):
    graph = catalog.build_graph(indexed_repo)
    assert len(graph["nodes"]) == 3
    edges = {(e["from"], e["to"]): e["reason"] for e in graph["edges"]}
    assert edges[("specs/billing/refund-flow.md", "specs/billing/refund-limits.md")] == "tags: refunds"


def test_build_graph_follows_related_references(indexed_repo):
    edges = catalog.build_graph(indexed_repo)["edges"]
    assert ("specs/search/indexing.md", "specs/billing/refund-flow.md", "related") in [
        (e["from"], e["to"], e["reason"]) for e in edges
    ]


def test_build_graph_does_not_duplicate_an_edge(indexed_repo, write_doc):
    """A `related:` pointer to a doc already linked by a shared tag adds no second edge."""
    write_doc(
        "billing/refund-flow.md",
        "# Billing — Refund Flow\n",
        {"type": "workflow", "tags": ["refunds"], "related": ["billing/refund-limits"]},
    )
    run_index(indexed_repo)
    pairs = [frozenset((e["from"], e["to"])) for e in catalog.build_graph(indexed_repo)["edges"]]
    assert len(pairs) == len(set(pairs))


def test_to_mermaid_emits_a_node_per_doc_and_typed_arrows(indexed_repo):
    mermaid = catalog.to_mermaid(catalog.build_graph(indexed_repo))
    assert mermaid.startswith("flowchart LR")
    assert mermaid.count(":::feature") == 2
    assert mermaid.count(":::workflow") == 1
    assert "-.->" in mermaid  # a `related` edge
    assert "-->" in mermaid  # a shared-tag edge


def test_to_mermaid_ids_are_derived_from_the_doc_path(indexed_repo):
    mermaid = catalog.to_mermaid(catalog.build_graph(indexed_repo))
    assert "specs_billing_refund_flow[" in mermaid


def test_mermaid_labels_escape_what_would_end_the_label_and_nothing_else(tmp_repo, write_doc):
    write_doc(
        "billing/hostile.md",
        '# Billing [v2] (draft) | #edge <b> "quoted"\n\nProse.\n',
        {"type": "feature", "tags": ["refunds"]},
    )
    run_index(tmp_repo)
    label = catalog.to_mermaid(catalog.build_graph(tmp_repo)).split('["')[1].split('"]')[0]

    assert '"' not in label and "]" not in label
    # Everything a quoted label already survives is left readable rather than entity-encoded.
    assert label == "Billing [v2#93; (draft) | #edge <b> #quot;quoted#quot;"


def test_a_bracket_in_a_title_no_longer_truncates_its_own_label(tmp_repo, write_doc):
    """The whole point, and only the renderer proves it: `Billing [v2] (draft)` used to come out
    as a node reading `"Billing [v2` — everything from the `]` on lost, plus a stray quote, with
    no error to notice."""
    from specky.html_render import render_mermaid_svg
    from specky.mermaid_tool import tool_dir

    if tool_dir() is None:
        pytest.skip("mermaid tool not installed — run `specky setup-diagrams`")

    write_doc(
        "billing/hostile.md",
        "# Billing [v2] (draft)\n\nProse.\n",
        {"type": "feature", "tags": ["refunds"]},
    )
    run_index(tmp_repo)

    svg = render_mermaid_svg(catalog.to_mermaid(catalog.build_graph(tmp_repo)))
    assert svg is not None and svg.lstrip().startswith("<svg")
    assert "(draft)" in svg  # the tail of the title survived the `]`
    assert "&quot;Billing" not in svg  # ...and the label isn't a half-parsed string any more


def test_commit_info_and_commits_for_doc(indexed_repo):
    conn = db.connect(indexed_repo)
    conn.execute(
        "INSERT INTO commits (sha, author, date, message) VALUES (?,?,?,?)",
        ("a" * 40, "Test <t@example.com>", "2026-01-01", "feat: refunds"),
    )
    conn.execute(
        "INSERT INTO commit_links (sha, path) VALUES (?, ?)",
        ("a" * 40, "specs/billing/refund-flow.md"),
    )
    conn.commit()
    conn.close()

    info = catalog.commit_info(indexed_repo, "a" * 40)
    assert info["tags"] == ["billing", "refunds"]
    assert [d["path"] for d in info["docs"]] == ["specs/billing/refund-flow.md"]

    commits = catalog.commits_for_doc(indexed_repo, "specs/billing/refund-flow.md")
    assert [c["message"] for c in commits] == ["feat: refunds"]


def test_commit_info_for_an_unlinked_sha(indexed_repo):
    info = catalog.commit_info(indexed_repo, "deadbeef")
    assert info == {"sha": "deadbeef", "tags": [], "docs": []}
