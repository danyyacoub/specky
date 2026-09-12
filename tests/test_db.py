import pytest

from specky import db


@pytest.mark.parametrize("text", ["", "   ", "...", "***", "-", "!!!"])
def test_no_searchable_tokens_returns_none(text):
    assert db.fts_match_query(text) is None


def test_single_token_is_quoted_as_a_phrase():
    assert db.fts_match_query("refunds") == '"refunds"'


def test_words_are_ored_for_recall():
    assert db.fts_match_query("refund flow") == '"refund" OR "flow"'


@pytest.mark.parametrize(
    "text, expected",
    [
        # FTS5 operator syntax in the input must survive as literal text, not as operators.
        ("db.py", '"db" OR "py"'),
        ("fts5-syntax", '"fts5" OR "syntax"'),
        ("spec*", '"spec"'),
        ("a NEAR b", '"a" OR "NEAR" OR "b"'),
        ("this AND that", '"this" OR "AND" OR "that"'),
        ('"quoted"', '"quoted"'),
    ],
)
def test_operator_syntax_is_neutralized(text, expected):
    assert db.fts_match_query(text) == expected


def test_match_query_is_usable_against_a_real_fts_table(tmp_repo):
    conn = db.connect(tmp_repo)
    try:
        conn.execute(
            "INSERT INTO documents_fts (path, domain, title, content, tags) VALUES (?,?,?,?,?)",
            ("specs/billing/refund-flow.md", "billing", "Refund flow", "issue a refund", "refunds"),
        )
        match = db.fts_match_query("refund* AND db.py")
        rows = conn.execute(
            "SELECT path FROM documents_fts WHERE documents_fts MATCH ?", (match,)
        ).fetchall()
        assert rows == [("specs/billing/refund-flow.md",)]
    finally:
        conn.close()


def test_connect_is_idempotent_and_creates_expected_tables(tmp_repo):
    db.connect(tmp_repo).close()
    conn = db.connect(tmp_repo)
    try:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        }
    finally:
        conn.close()
    assert {"documents", "documents_fts", "commits", "commits_fts", "micro_docs", "commit_links"} <= names


def test_the_index_is_opened_in_wal_with_a_busy_timeout(tmp_repo):
    conn = db.connect(tmp_repo)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_a_reader_and_a_writer_can_hold_the_index_at_the_same_time(tmp_repo):
    """The post-commit hook writes micro_docs into the same file a running `specky serve` is
    reading, and `specky index` rewrites both FTS tables wholesale. Under the default rollback
    journal, an open read transaction blocks the writer — it waits out `busy_timeout` and then
    fails with `database is locked`, which is a broken hook or a 500 from the chat server for no
    reason the user can act on. WAL is what makes both sides succeed."""
    reader = db.connect(tmp_repo)
    writer = db.connect(tmp_repo)
    try:
        writer.execute("INSERT INTO micro_docs (sha, summary, created_at) VALUES ('a','one','t')")
        writer.commit()

        reader.execute("BEGIN")  # deferred; the SELECT below is what takes the read lock
        assert reader.execute("SELECT COUNT(*) FROM micro_docs").fetchone()[0] == 1

        writer.execute("INSERT INTO micro_docs (sha, summary, created_at) VALUES ('b','two','t')")
        writer.commit()

        # The reader keeps its snapshot until it ends its transaction, then sees the new row.
        assert reader.execute("SELECT COUNT(*) FROM micro_docs").fetchone()[0] == 1
        reader.rollback()
        assert reader.execute("SELECT COUNT(*) FROM micro_docs").fetchone()[0] == 2
    finally:
        reader.close()
        writer.close()


def test_migrate_recreates_an_fts_table_with_a_stale_column_set(tmp_repo):
    """FTS5 tables can't be ALTERed, so `_migrate` drops and recreates one whose columns
    drifted — the next `specky index` refills it."""
    conn = db.connect(tmp_repo)
    conn.execute("DROP TABLE documents_fts")
    conn.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(path, domain, title, content)")
    conn.commit()
    conn.close()

    conn = db.connect(tmp_repo)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(documents_fts)")]
    finally:
        conn.close()
    assert columns == ["path", "domain", "title", "content", "tags"]
