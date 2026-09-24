"""Read-only queries over classified feature/workflow docs — powers `specky
features`/`workflows`/`tags`/`graph` and their MCP-tool equivalents. Reads from the
`documents` table, so `specky index` must have run at least once, same precondition
`specky search` already has.
"""

from __future__ import annotations

import re
from pathlib import Path

from specky import paths
from specky.commit_doc import history_doc_for
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
    # Keyed by `<domain>/<topic>.md`, which is the spelling a `related:` entry uses — the docs
    # root's own name never appears in one, and needn't be known here to resolve it.
    by_path = {d["path"].split("/", 1)[-1]: d for d in docs}

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
            target = by_path.get(f"{rel_topic}.md")
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
    commit_info().

    Each carries what its history doc says — the one-line `headline`, its `impact`, and the doc's
    repo path as `history_path` — so a caller listing a feature's recent changes has a sentence to
    show rather than a commit subject. All three are empty for a commit with no structured doc.
    """
    conn = connect(repo_root)
    try:
        rows = conn.execute(
            "SELECT commits.sha, commits.author, commits.date, commits.message, "
            "commits.headline, commits.impact "
            "FROM commit_links JOIN commits ON commits.sha = commit_links.sha "
            "WHERE commit_links.path = ? ORDER BY commits.date DESC",
            (doc_path,),
        ).fetchall()
    finally:
        conn.close()
    history_dir = paths.history_dir(repo_root)
    commits = []
    for sha, author, date, message, headline, impact in rows:
        doc = history_doc_for(history_dir, sha)
        commits.append(
            {
                "sha": sha,
                "author": author,
                "date": date,
                "message": message,
                "headline": headline,
                "impact": impact,
                "history_path": str(doc.relative_to(repo_root)) if doc else "",
            }
        )
    return commits


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


# --- the tag registry ----------------------------------------------------------------------------
# Everything above reads the index. What follows reads the worktree, because its readers can't wait
# for one: `specky lint` runs on docs an agent wrote a minute ago, and `specky tags --write` seeds a
# registry on a repo that may never have been indexed.


def docs_on_disk(repo_root: Path) -> list[tuple[str, dict, str]]:
    """Every topic doc under the docs root as `(repo-relative path, frontmatter, body)`.

    Topic docs only: `history/` is one generated doc per commit, and the root's own files
    (GLOSSARY.md, MODULES.md, PRODUCT.md, TAGS.md) describe the tree rather than a feature — the same
    split `generator.ExistingDocs` makes.
    """
    from specky import frontmatter

    root = paths.docs_root(repo_root)
    if not root.exists():
        return []
    history = paths.history_dir(repo_root)
    docs = []
    for path in sorted(root.rglob("*.md")):
        if path.parent == root or history in path.parents:
            continue
        meta, body = frontmatter.parse(path.read_text(errors="replace"))
        docs.append((path.relative_to(repo_root).as_posix(), meta, body))
    return docs


def doc_tags(meta: dict) -> list[str]:
    tags = meta.get("tags")
    return [t for t in tags if t] if isinstance(tags, list) else []


def load_tag_registry(repo_root: Path) -> dict[str, str]:
    """`TAGS.md`'s `| **tag** | meaning |` rows, or `{}` when the repo keeps no registry.

    No registry means no enforcement anywhere — `lint` and `check` only flag a tag outside the
    registry when there is one — so a repo opts in by creating the file, not by configuring specky.
    """
    return paths.read_term_table(paths.tags_registry(repo_root))


def _usage(docs: list[str], root_prefix: str) -> str:
    names = [d.removeprefix(root_prefix).removesuffix(".md") for d in docs]
    shown = ", ".join(names[:3])
    return f"Used by {shown}" + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def write_tag_registry(repo_root: Path) -> tuple[Path, list[str]]:
    """Create or extend `TAGS.md` with every tag in use that it doesn't list yet.

    Additive like `generator.append_glossary_rows`, and for the same reason: it's a file people
    curate. A new row's meaning is a placeholder naming the docs that use the tag — true, and
    obviously not a definition, so the person reviewing the diff knows what to replace. The rows
    this seeds are the tags as they are, sprawl included: the registry is where a human merges
    `document-matching` into `matching`, and seeding it is what puts that list in front of them.
    """
    path = paths.tags_registry(repo_root)
    known = {tag.lower() for tag in load_tag_registry(repo_root)}
    in_use: dict[str, list[str]] = {}
    for doc_path, meta, _ in docs_on_disk(repo_root):
        for tag in doc_tags(meta):
            in_use.setdefault(tag, []).append(doc_path)
    root_prefix = paths.docs_prefix(repo_root)
    added = [tag for tag in sorted(in_use) if tag.lower() not in known and "|" not in tag and "*" not in tag]
    if not added:
        return path, []
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {repo_root.name} — Tags\n\n"
            "The tag vocabulary for the docs in this tree. A doc's `tags:` come from this list, so "
            "docs about one concept share one tag and group together in search, the viewer and "
            "`specky graph`. `specky lint` flags a tag that isn't here: reuse one of these, or add "
            "a row when a genuinely new business concept needs its own.\n\n"
            "| Tag | Meaning |\n|---|---|\n"
        )
    lines = path.read_text().splitlines()
    last_row = max((i for i, line in enumerate(lines) if line.startswith("|")), default=len(lines) - 1)
    lines[last_row + 1 : last_row + 1] = [
        f"| **{tag}** | {_usage(in_use[tag], root_prefix)} |" for tag in added
    ]
    path.write_text("\n".join(lines).rstrip("\n") + "\n")
    return path, added
