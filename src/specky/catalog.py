"""Read-only queries over classified feature/workflow docs — powers `specky
features`/`workflows`/`tags`/`graph` and their MCP-tool equivalents. Reads from the
`documents` table, so `specky index` must have run at least once, same precondition
`specky search` already has.
"""

from __future__ import annotations

import re
from pathlib import Path

from specky.db import connect


def _classified_docs(repo_root: Path) -> list[dict]:
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT path, domain, title, doc_type, tags, related, owner FROM documents "
            "WHERE doc_type != '' ORDER BY domain, title"
        ).fetchall()
        commit_counts = dict(conn.execute("SELECT path, COUNT(*) FROM commit_links GROUP BY path"))
    finally:
        conn.close()
    return [
        {
            "path": path,
            "domain": domain,
            "title": title,
            "type": doc_type,
            "tags": [t for t in tags.split(",") if t],
            "related": [r for r in related.split(",") if r],
            "owner": owner,
            "commits": commit_counts.get(path, 0),
        }
        for path, domain, title, doc_type, tags, related, owner in rows
    ]


def list_features(repo_root: Path) -> list[dict]:
    return [d for d in _classified_docs(repo_root) if d["type"] == "feature"]


def list_workflows(repo_root: Path) -> list[dict]:
    return [d for d in _classified_docs(repo_root) if d["type"] == "workflow"]


def list_tags(repo_root: Path) -> dict[str, list[dict]]:
    """Every tag in use, mapped to the docs carrying it, alphabetical by tag."""
    by_tag: dict[str, list[dict]] = {}
    for doc in _classified_docs(repo_root):
        for tag in doc["tags"]:
            by_tag.setdefault(tag, []).append(doc)
    return dict(sorted(by_tag.items()))


def _node_id(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", path.removesuffix(".md")).strip("_")


def build_graph(repo_root: Path) -> dict:
    """Nodes are every classified doc; edges connect a workflow to a feature that shares
    >=1 tag, plus one edge per hand-authored `related:` reference not already covered by a
    shared tag."""
    docs = _classified_docs(repo_root)
    by_path = {d["path"]: d for d in docs}

    nodes = [
        {"id": _node_id(d["path"]), "path": d["path"], "title": d["title"], "type": d["type"]} for d in docs
    ]

    edges: list[dict] = []
    seen: set[frozenset[str]] = set()

    def add_edge(a: dict, b: dict, reason: str) -> None:
        key = frozenset((a["path"], b["path"]))
        if key in seen:
            return
        seen.add(key)
        edges.append({"from": a["path"], "to": b["path"], "reason": reason})

    features = [d for d in docs if d["type"] == "feature"]
    workflows = [d for d in docs if d["type"] == "workflow"]
    for workflow in workflows:
        for feature in features:
            shared = set(workflow["tags"]) & set(feature["tags"])
            if shared:
                add_edge(workflow, feature, f"tags: {', '.join(sorted(shared))}")

    for doc in docs:
        for rel_topic in doc["related"]:
            target = by_path.get(f"specs/{rel_topic}.md")
            if target:
                add_edge(doc, target, "related")

    return {"nodes": nodes, "edges": edges}


def commit_info(repo_root: Path, sha: str) -> dict:
    """Tags and feature/workflow docs linked to a single commit, via commit_links."""
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT documents.path, documents.title, documents.doc_type, documents.tags "
            "FROM commit_links JOIN documents ON documents.path = commit_links.path "
            "WHERE commit_links.sha = ?",
            (sha,),
        ).fetchall()
    finally:
        conn.close()

    docs = [
        {"path": path, "title": title, "type": doc_type, "tags": [t for t in tags.split(",") if t]}
        for path, title, doc_type, tags in rows
    ]
    tags = sorted({tag for doc in docs for tag in doc["tags"]})
    return {"sha": sha, "tags": tags, "docs": docs}


def commits_for_doc(repo_root: Path, doc_path: str) -> list[dict]:
    """Commits linked to a given feature/workflow doc, most recent first — the reverse of
    commit_info()."""
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT commits.sha, commits.author, commits.date, commits.message "
            "FROM commit_links JOIN commits ON commits.sha = commit_links.sha "
            "WHERE commit_links.path = ? ORDER BY commits.date DESC",
            (doc_path,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"sha": sha, "author": author, "date": date, "message": message}
        for sha, author, date, message in rows
    ]


def _mermaid_label(title: str) -> str:
    """A doc title is arbitrary prose, and it goes inside a `["…"]` node label.

    Quoting is mermaid's own escape mechanism and covers almost everything: `(`, `)`, `{`, `}`,
    `|`, `<`, `>` and `#` all render literally, in real mermaid and in the vendored renderer
    `render-html` uses. Two don't, and both fail silently:

    - `]` — the vendored renderer ends the label there and swallows the rest of the line, so a
      title containing one drops that node's neighbours out of the diagram with no error anywhere.
    - `"` — closes the label in any renderer.

    Both become mermaid entity codes, which real mermaid substitutes back when it draws the label
    (the vendored renderer prints them literally, which is why nothing else is escaped: a title
    with a paren in it should read as a paren, not as `#40;`).
    """
    return title.replace("]", "#93;").replace('"', "#quot;")


def to_mermaid(graph: dict) -> str:
    lines = [
        "flowchart LR",
        "  classDef feature fill:#eef1ff,stroke:#4f46e5",
        "  classDef workflow fill:#fff7ed,stroke:#b45309",
    ]
    for node in graph["nodes"]:
        lines.append(f'  {node["id"]}["{_mermaid_label(node["title"])}"]:::{node["type"]}')

    node_id_by_path = {n["path"]: n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        arrow = "-.->" if edge["reason"] == "related" else "-->"
        lines.append(f'  {node_id_by_path[edge["from"]]} {arrow} {node_id_by_path[edge["to"]]}')

    return "\n".join(lines)
