"""`specky bootstrap` over a real repo: what it reads, what it refuses to read, and what it writes.

Two halves. The first needs no provider at all — the repo map is deterministic, and every landmine
in it (binaries, symlinks, minified bundles, non-UTF-8 source) is a file on disk and an assertion.
The second uses `RoutingProvider`, which answers by what the prompt asks for rather than by call
order, because bootstrap makes several different kinds of call in one run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from specky import bootstrap, frontmatter

from conftest import RoutingProvider, git


def _src(repo: Path, rel: str, body: str = "def hello():\n    return 1\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _commit(repo: Path, message: str = "add source") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def _discovery(*domains: dict, product: dict | None = None) -> str:
    return json.dumps(
        {"product": product or {"what_it_is": "A thing that does things."}, "domains": list(domains)}
    )


def _domain(domain="billing", topic="refund-flow", paths=("src/billing/refund.py",), **kw) -> dict:
    return {
        "domain": domain,
        "topic": topic,
        "purpose": kw.get("purpose", "Issue refunds"),
        "type": kw.get("type", "feature"),
        "tags": kw.get("tags", ["billing"]),
        "paths": list(paths),
    }


# --- the repo map: no provider involved ---------------------------------------------------------


def test_a_gitignored_file_is_invisible_to_the_map(tmp_repo):
    """The load-bearing property of enumerating with `git ls-files` rather than an rglob: a
    node_modules/ or .venv/ is excluded by construction, not by a denylist per ecosystem."""
    _src(tmp_repo, "src/real.py")
    _src(tmp_repo, "node_modules/pkg/index.js", "module.exports = 1;\n")
    (tmp_repo / ".gitignore").write_text("node_modules/\n")
    _commit(tmp_repo)

    files = bootstrap.source_files(tmp_repo)
    assert "src/real.py" in files
    assert not any("node_modules" in f for f in files)


def test_a_cp1252_source_file_does_not_abort_the_map(tmp_repo):
    """Git hands back a non-UTF-8 file with no early NUL as text, and a strict decode would abort
    the whole run over one file — the landmine `commit_doc._commit_info` already documents."""
    path = _src(tmp_repo, "src/cafe.py")
    path.write_bytes(b"# caf\xe9\ndef order():\n    return 1\n")
    _commit(tmp_repo)

    assert "�" in bootstrap.read_source(path)
    assert bootstrap.repo_map(tmp_repo).symbols  # and the map still builds


def test_a_binary_file_with_a_source_extension_is_skipped(tmp_repo):
    path = _src(tmp_repo, "src/blob.py")
    path.write_bytes(b"\x89PNG\x00\x00\x00\rIHDR" + b"\xff" * 200)
    _commit(tmp_repo)

    assert bootstrap.read_source(path) is None


def test_a_minified_bundle_is_never_read(tmp_repo):
    path = _src(tmp_repo, "src/vendor.js", "var a=1;" * 40_000)  # one enormous line
    _commit(tmp_repo)

    assert bootstrap.read_source(path) is None


def test_an_oversized_file_is_never_read(tmp_repo):
    path = _src(tmp_repo, "src/big.py", "# padding\n" * (bootstrap.MAX_FILE_BYTES // 5))
    _commit(tmp_repo)

    assert bootstrap.read_source(path) is None


def test_a_symlink_is_never_read_as_source(tmp_repo):
    _src(tmp_repo, "src/real.py")
    link = tmp_repo / "src" / "alias.py"
    link.symlink_to("real.py")
    _commit(tmp_repo)

    assert bootstrap.read_source(link) is None


def test_the_symbol_index_stays_inside_its_budget(tmp_repo):
    """600 symbol-heavy files across 20 directories: the index is budgeted, and what it left out is
    declared — a silently partial map reads to the model as a complete one."""
    body = '"""A module."""\n\n' + "".join(f"def run{i}():\n    pass\n\n" for i in range(30))
    for d in range(20):
        for f in range(30):
            _src(tmp_repo, f"pkg{d}/mod{f}.py", body)
    _commit(tmp_repo)

    rmap = bootstrap.repo_map(tmp_repo)
    assert len(rmap.files) == 600
    assert len(rmap.symbols) <= bootstrap.SYMBOL_BUDGET
    assert rmap.skipped
    assert "not listed here" in rmap.as_prompt()


def test_every_directory_is_represented_even_beside_a_huge_one(tmp_repo):
    """The point of filling round-robin rather than directory by directory: you cannot discover a
    domain you were never shown, so `giant/` must not spend the whole budget before `tiny/`."""
    for f in range(300):
        _src(tmp_repo, f"giant/mod{f}.py")
    _src(tmp_repo, "tiny/billing.py")
    _commit(tmp_repo)

    assert "tiny/billing.py" in bootstrap.repo_map(tmp_repo).symbols


def test_scoping_excludes_everything_outside_the_subtree(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py")
    _src(tmp_repo, "src/auth/login.py")
    _commit(tmp_repo)

    assert bootstrap.source_files(tmp_repo, scope="src/billing") == ["src/billing/refund.py"]


def test_python_symbols_come_from_ast_and_skip_private_names(tmp_repo):
    text = '"""What this module is."""\n\ndef public():\n    pass\n\ndef _private():\n    pass\n'
    summary = bootstrap.file_summary("src/a.py", text)
    assert "What this module is." in summary
    assert "public" in summary and "_private" not in summary


# --- resolving what discovery named --------------------------------------------------------------


def test_a_directory_prefix_expands_to_its_files():
    files = ["src/billing/refund.py", "src/billing/ledger.py", "src/auth/login.py"]
    assert bootstrap.resolve_paths(["src/billing/"], files) == [
        "src/billing/refund.py",
        "src/billing/ledger.py",
    ]


def test_a_path_that_does_not_exist_is_dropped():
    """The guard against the worst failure available here — a doc written from no source at all is
    fabricated end to end and reads exactly like a real one."""
    assert bootstrap.resolve_paths(["src/payments/"], ["lib/billing/refund.py"]) == []


# --- the run ---------------------------------------------------------------------------------------


def test_a_repo_with_no_feature_docs_is_cold(tmp_repo, write_doc):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)

    assert bootstrap.needs_bootstrap(tmp_repo)
    write_doc("history/abc12345.md", "# Commit abc12345\n")
    write_doc("MODULES.md", "# Modules\n")
    assert bootstrap.needs_bootstrap(tmp_repo), "history and root docs must not count"

    write_doc("billing/refund-flow.md", "# Billing\n", {"type": "feature", "tags": ["billing"]})
    assert not bootstrap.needs_bootstrap(tmp_repo)


def test_a_repo_with_no_source_is_not_cold(tmp_repo):
    """An all-prose or empty repo must not be announced as needing a bootstrap and then reported as
    having nothing to read — that was two lines of noise on every `specky sync`."""
    assert not bootstrap.needs_bootstrap(tmp_repo)


def test_it_writes_a_doc_product_and_glossary(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    provider = RoutingProvider(
        discovery=_discovery(_domain()),
        doc="# Billing — Refund Flow\n\n## What It Does\nIssues refunds.\n",
        glossary=json.dumps({"terms": [{"term": "Refund", "definition": "Money returned."}]}),
    )

    written = bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    doc = tmp_repo / "specs" / "billing" / "refund-flow.md"
    assert doc in written
    meta, body = frontmatter.parse(doc.read_text())
    assert meta["type"] == "feature" and meta["tags"] == ["billing"]
    assert meta["sources"] == ["src/billing/refund.py"]
    assert "Issues refunds." in body
    assert "## What it is" in (tmp_repo / "specs" / "PRODUCT.md").read_text()
    assert "| **Refund** |" in (tmp_repo / "specs" / "GLOSSARY.md").read_text()
    assert "billing/refund-flow.md" in (tmp_repo / "specs" / "MODULES.md").read_text()


def test_a_generated_glossary_row_round_trips_through_the_viewers_parser(tmp_repo):
    """GLOSSARY.md has a machine contract on the other side: `html_render.load_glossary` parses it
    back to drive term auto-linking, and degrades silently to a no-op on a row it can't match."""
    from specky.html_render import load_glossary

    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    terms = [
        {"term": "Refund window", "definition": "The period a refund may be requested in."},
        {"term": "Chargeback", "definition": "A reversal forced by the card issuer | the bank."},
    ]
    provider = RoutingProvider(
        discovery=_discovery(_domain()), glossary=json.dumps({"terms": terms})
    )

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    parsed = load_glossary(tmp_repo)
    assert "Refund window" in parsed
    assert "Chargeback" in parsed, "a pipe in a definition must not break the row"


def test_hand_written_glossary_definitions_win(tmp_repo, write_doc):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    write_doc("GLOSSARY.md", "# Glossary\n\n| Term | Definition |\n|---|---|\n| **Refund** | Mine. |\n")
    provider = RoutingProvider(
        discovery=_discovery(_domain()),
        glossary=json.dumps({"terms": [{"term": "Refund", "definition": "Theirs."}]}),
    )

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    assert "Mine." in (tmp_repo / "specs" / "GLOSSARY.md").read_text()
    assert "Theirs." not in (tmp_repo / "specs" / "GLOSSARY.md").read_text()


def test_product_md_is_never_overwritten(tmp_repo, write_doc):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    write_doc("PRODUCT.md", "# Hand written\n")
    provider = RoutingProvider(discovery=_discovery(_domain()))

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    assert (tmp_repo / "specs" / "PRODUCT.md").read_text() == "# Hand written\n"


def test_max_domains_caps_the_run_and_reports_the_rest(tmp_repo, capsys):
    for name in ("a", "b", "c"):
        _src(tmp_repo, f"src/{name}/mod.py")
    _commit(tmp_repo)
    provider = RoutingProvider(
        discovery=_discovery(*(_domain(domain=n, topic=n, paths=(f"src/{n}/mod.py",)) for n in "abc"))
    )

    bootstrap.bootstrap(tmp_repo, provider, max_domains=2, assume_yes=True)

    assert len(list((tmp_repo / "specs").glob("*/*.md"))) == 2
    assert "1 more domain(s) not documented" in capsys.readouterr().out


def test_a_second_run_documents_only_what_is_new(tmp_repo):
    for name in ("a", "b", "c"):
        _src(tmp_repo, f"src/{name}/mod.py")
    _commit(tmp_repo)
    discovery = _discovery(
        *(_domain(domain=n, topic=n, paths=(f"src/{n}/mod.py",)) for n in "abc")
    )

    bootstrap.bootstrap(tmp_repo, RoutingProvider(discovery=discovery), max_domains=2, assume_yes=True)
    first = {p.name for p in (tmp_repo / "specs").glob("*/*.md")}
    second = RoutingProvider(discovery=discovery)
    bootstrap.bootstrap(tmp_repo, second, max_domains=2, assume_yes=True)

    assert {p.name for p in (tmp_repo / "specs").glob("*/*.md")} == first | {"c.md"}


def test_a_third_run_with_nothing_left_says_so(tmp_repo, capsys):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    discovery = _discovery(_domain())
    bootstrap.bootstrap(tmp_repo, RoutingProvider(discovery=discovery), assume_yes=True)

    provider = RoutingProvider(discovery=discovery)
    assert bootstrap.bootstrap(tmp_repo, provider, assume_yes=True) == []
    assert "every domain already has a doc" in capsys.readouterr().out


def test_the_discovery_prompt_lists_docs_that_already_exist(tmp_repo, write_doc):
    """The anti-duplicate guard: without it, run 2 renames `billing` to `billing-and-invoicing`
    and writes a second doc beside the first."""
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    write_doc("billing/refund-flow.md", "# Billing\n", {"type": "feature", "tags": ["billing"]})
    provider = RoutingProvider(discovery=_discovery())

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    assert "billing/refund-flow" in provider.prompts[0]


def test_a_domain_naming_no_real_source_is_skipped_without_a_doc_call(tmp_repo, capsys):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    provider = RoutingProvider(discovery=_discovery(_domain(paths=("src/nonexistent/",))))

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    assert not (tmp_repo / "specs" / "billing" / "refund-flow.md").exists()
    assert "named no source that exists" in capsys.readouterr().out
    assert len(provider.prompts) == 1, "only discovery should have been paid for"


def test_the_domain_prompt_carries_its_own_source(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py", "def refund_order():\n    return 'refunded'\n")
    _src(tmp_repo, "src/auth/login.py", "def log_in():\n    return 'ok'\n")
    _commit(tmp_repo)
    provider = RoutingProvider(discovery=_discovery(_domain()))

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    doc_prompt = next(p for p in provider.prompts if "Source for this feature" in p)
    assert "refund_order" in doc_prompt
    assert "log_in" not in doc_prompt, "another domain's source must not ride along"


def test_a_huge_file_is_shown_truncated_and_says_so(tmp_repo):
    _src(tmp_repo, "src/billing/refund.py", "def f():\n    pass\n" * 4_000)
    _commit(tmp_repo)
    provider = RoutingProvider(discovery=_discovery(_domain()))

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    doc_prompt = next(p for p in provider.prompts if "Source for this feature" in p)
    assert "…[truncated —" in doc_prompt


def test_a_failing_domain_does_not_abandon_the_rest(tmp_repo, capsys):
    for name in ("a", "b"):
        _src(tmp_repo, f"src/{name}/mod.py")
    _commit(tmp_repo)

    class Exploding(RoutingProvider):
        def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
            if "specs/a/a.md" in prompt:
                raise RuntimeError("provider is down")
            return super().generate(prompt, prefix=prefix, task=task)

    provider = Exploding(
        discovery=_discovery(*(_domain(domain=n, topic=n, paths=(f"src/{n}/mod.py",)) for n in "ab"))
    )
    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    assert (tmp_repo / "specs" / "b" / "b.md").exists()
    assert "provider is down" in capsys.readouterr().out


def test_dry_run_lists_the_plan_and_writes_nothing(tmp_repo, capsys):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    provider = RoutingProvider(discovery=_discovery(_domain()))

    assert bootstrap.bootstrap(tmp_repo, provider, dry_run=True) == []

    assert not (tmp_repo / "specs" / "billing").exists()
    assert "billing/refund-flow" in capsys.readouterr().out
    assert len(provider.prompts) == 1, "dry-run still costs the discovery call, and only that"


def test_a_repo_with_no_source_calls_nothing(tmp_repo, capsys):
    provider = RoutingProvider()

    assert bootstrap.bootstrap(tmp_repo, provider, assume_yes=True) == []

    assert provider.prompts == []
    assert "no source files found" in capsys.readouterr().out


def test_declared_sources_give_a_bootstrapped_repo_check_coverage(tmp_repo):
    """Without `sources:`, `indexer.index_doc_files` derives coverage from git log alone — and its
    DOC_FILES_MAX_DOCS_PER_COMMIT drops a commit that adds a whole doc tree, leaving `specky check`
    silently off on exactly the repos that most need it."""
    from specky.db import connect
    from specky.indexer import run_index

    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    bootstrap.bootstrap(tmp_repo, RoutingProvider(discovery=_discovery(_domain())), assume_yes=True)
    _commit(tmp_repo, "docs")
    run_index(tmp_repo)

    conn = connect(tmp_repo)
    try:
        rows = conn.execute(
            "SELECT doc_path FROM doc_files WHERE path = ?", ("src/billing/refund.py",)
        ).fetchall()
    finally:
        conn.close()
    assert ("specs/billing/refund-flow.md",) in rows


def test_a_later_commit_update_does_not_drop_the_sources_line(tmp_repo, write_doc):
    """The hook runs on every commit, so a `sources:` the first update erases is a coverage
    regression that lands within a day of bootstrap."""
    from specky.commit_doc import Commit
    from specky.generator import sync_feature_doc

    write_doc(
        "billing/refund-flow.md",
        "# Billing\n\n## What It Does\n\nRefunds.\n",
        {"type": "feature", "tags": ["billing"], "sources": ["src/billing/refund.py"]},
    )
    provider = RoutingProvider(
        classification=json.dumps(
            {"skip": False, "domain": "billing", "topic": "refund-flow", "purpose": "Refunds",
             "type": "feature", "tags": ["billing"]}
        ),
        doc=json.dumps({"sections": {}}),
    )
    commit = Commit(sha="a" * 40, author="t", date="2026-01-01", message="tweak", diff="diff")

    sync_feature_doc(tmp_repo, commit, provider)

    meta, _ = frontmatter.parse((tmp_repo / "specs" / "billing" / "refund-flow.md").read_text())
    assert meta["sources"] == ["src/billing/refund.py"]


def test_tracked_vendored_code_is_excluded(tmp_repo):
    """gitignore can't help here — a Go `vendor/` or a committed `node_modules/` is tracked, so it
    would otherwise compete with real code for the map's budget."""
    _src(tmp_repo, "src/app.py")
    _src(tmp_repo, "vendor/dep/lib.py")
    _src(tmp_repo, "src/api_pb2.py")
    _src(tmp_repo, "web/bundle.min.js", "var a=1;\n")
    _commit(tmp_repo)

    assert bootstrap.source_files(tmp_repo) == ["src/app.py"]


def test_tests_rank_behind_real_code(tmp_repo):
    _src(tmp_repo, "src/billing.py")
    _src(tmp_repo, "tests/test_billing.py")
    _commit(tmp_repo)

    symbols = bootstrap.repo_map(tmp_repo).symbols
    assert symbols.index("src/billing.py") < symbols.index("tests/test_billing.py")


def test_an_entry_point_is_listed_before_its_siblings(tmp_repo):
    for name in ("zebra", "__init__", "alpha"):
        _src(tmp_repo, f"pkg/{name}.py")
    _commit(tmp_repo)

    symbols = bootstrap.repo_map(tmp_repo).symbols
    assert symbols.index("pkg/__init__.py") < symbols.index("pkg/alpha.py")


def test_a_truncated_discovery_response_names_the_lever(tmp_repo):
    """Discovery is the one call here whose truncation is fatal, and it's also the most expensive —
    so the error has to say which knob fixes it."""
    from specky.ai_provider import TruncatedResponse

    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)

    class Truncating(RoutingProvider):
        def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
            raise TruncatedResponse("hit the 4096-token output limit")

    with pytest.raises(RuntimeError, match="max_tokens"):
        bootstrap.discover(
            tmp_repo,
            Truncating(),
            bootstrap.repo_map(tmp_repo),
            __import__("specky.generator", fromlist=["x"]).ExistingDocs(),
        )


# --- batch --------------------------------------------------------------------------------------


class BatchingProvider(RoutingProvider):
    """A RoutingProvider that can also answer a whole batch, recording what it was sent."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.batches: list[dict] = []

    def generate_batch(self, prompts, task: str = ""):
        self.batches.append(prompts)
        return {key: self._doc for key in prompts}


def test_batch_sends_every_domain_in_one_request(tmp_repo):
    for name in ("a", "b", "c"):
        _src(tmp_repo, f"src/{name}/mod.py")
    _commit(tmp_repo)
    provider = BatchingProvider(
        discovery=_discovery(*(_domain(domain=n, topic=n, paths=(f"src/{n}/mod.py",)) for n in "abc")),
        doc="# D\n\n## What It Does\nThings.\n",
    )

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True, batch=True)

    assert len(provider.batches) == 1
    assert set(provider.batches[0]) == {"a/a", "b/b", "c/c"}
    assert len(list((tmp_repo / "specs").glob("*/*.md"))) == 3


def test_batch_entries_share_one_cacheable_prefix(tmp_repo):
    for name in ("a", "b"):
        _src(tmp_repo, f"src/{name}/mod.py")
    _commit(tmp_repo)
    provider = BatchingProvider(
        discovery=_discovery(*(_domain(domain=n, topic=n, paths=(f"src/{n}/mod.py",)) for n in "ab"))
    )

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True, batch=True)

    prefixes = {prefix for prefix, _ in provider.batches[0].values()}
    assert len(prefixes) == 1, "the stable half must be identical across a batch"


def test_batch_falls_back_when_the_provider_has_none(tmp_repo, capsys):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)
    provider = RoutingProvider(discovery=_discovery(_domain()))  # no generate_batch

    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True, batch=True)

    assert (tmp_repo / "specs" / "billing" / "refund-flow.md").exists()
    assert "--batch ignored" in capsys.readouterr().out


def test_a_failed_batch_falls_back_to_one_call_per_domain(tmp_repo, capsys):
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)

    class Exploding(BatchingProvider):
        def generate_batch(self, prompts, task: str = ""):
            raise RuntimeError("batch API is down")

    provider = Exploding(discovery=_discovery(_domain()))
    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True, batch=True)

    assert (tmp_repo / "specs" / "billing" / "refund-flow.md").exists()
    assert "batch failed" in capsys.readouterr().out


def test_a_domain_missing_from_the_batch_result_is_generated_singly(tmp_repo):
    for name in ("a", "b"):
        _src(tmp_repo, f"src/{name}/mod.py")
    _commit(tmp_repo)

    class Partial(BatchingProvider):
        def generate_batch(self, prompts, task: str = ""):
            self.batches.append(prompts)
            return {"a/a": self._doc}  # b/b never came back

    provider = Partial(
        discovery=_discovery(*(_domain(domain=n, topic=n, paths=(f"src/{n}/mod.py",)) for n in "ab")),
        doc="# D\n\n## What It Does\nThings.\n",
    )
    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True, batch=True)

    assert (tmp_repo / "specs" / "b" / "b.md").exists()


def test_the_discovery_call_is_tagged_with_its_task(tmp_repo):
    """Per-task models route on this label, so a mislabelled call silently uses the default."""
    _src(tmp_repo, "src/billing/refund.py")
    _commit(tmp_repo)

    class TaskSpy(RoutingProvider):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.tasks = []

        def generate(self, prompt, *, prefix="", task=""):
            self.tasks.append(task)
            return super().generate(prompt, prefix=prefix, task=task)

    provider = TaskSpy(discovery=_discovery(_domain()))
    bootstrap.bootstrap(tmp_repo, provider, assume_yes=True)

    assert provider.tasks[0] == "discovery"
    assert "doc" in provider.tasks
    assert "glossary" in provider.tasks
