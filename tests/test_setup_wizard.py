"""`specky init` — the interview, and the flags that replace it.

The interactive path has a human to correct it. The non-interactive one doesn't, and it's the one a
setup script runs: a Devin blueprint step, a Dockerfile, a CI job priming a cache. So what's asserted
here is mostly about *not* half-succeeding — a missing flag has to be an error before specky.toml is
written, not a config file that only breaks on the first commit.
"""

from __future__ import annotations

import pytest

from specky import cli, setup_wizard
from specky.ai_provider import ConfigError
from specky.setup_wizard import InitOptions, run_init

NO_VALIDATE = {"validate": False}


@pytest.fixture(autouse=True)
def _claude_is_the_current_agent(monkeypatch):
    """Whatever this machine has on PATH, the tests see one agent: Claude Code."""
    monkeypatch.setattr(setup_wizard, "current_agent", lambda: "claude")


def _config(tmp_path, **options):
    """Run a non-interactive init and hand back the specky.toml it wrote."""
    path = tmp_path / "specky.toml"
    run_init(path, input_fn=_never_asks, print_fn=lambda _msg: None, options=InitOptions(**options))
    return path.read_text()


def _never_asks(prompt: str) -> str:
    raise AssertionError(f"non-interactive init asked a question: {prompt!r}")


# --- the flags ---------------------------------------------------------------------------


def test_yes_alone_writes_the_current_agent_on_its_default_model(tmp_path):
    """`specky init --yes` is the one-liner a setup script wants: the same answers the interview's
    defaults give, with nothing to type."""
    body = _config(tmp_path, assume_yes=True, **NO_VALIDATE)

    assert '[ai]' in body
    assert 'provider = "agent"' in body
    assert 'agent = "claude"' in body
    assert "model" not in body


def test_yes_without_an_agent_names_the_way_out(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "current_agent", lambda: None)
    with pytest.raises(ConfigError, match="--provider openai-compatible"):
        _config(tmp_path, assume_yes=True, **NO_VALIDATE)
    assert not (tmp_path / "specky.toml").exists()


def test_naming_a_provider_is_enough_to_skip_the_interview(tmp_path):
    """`--provider` implies `--yes`: the interview's whole job is finding out which provider."""
    assert InitOptions(provider="agent").non_interactive
    body = _config(tmp_path, provider="agent", model="opus", **NO_VALIDATE)

    assert 'model = "opus"' in body


def test_an_openai_compatible_provider_needs_its_endpoint(tmp_path):
    """Named in one error rather than one flag at a time, so a setup script's log says what to add
    in a single pass."""
    with pytest.raises(ConfigError) as exc:
        _config(tmp_path, provider="openai-compatible", **NO_VALIDATE)

    for flag in ("--base-url", "--model", "--api-key-env"):
        assert flag in str(exc.value)
    assert not (tmp_path / "specky.toml").exists(), "wrote a config it had already rejected"


def test_a_complete_openai_compatible_provider_round_trips(tmp_path):
    body = _config(
        tmp_path,
        provider="openai-compatible",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        **NO_VALIDATE,
    )

    assert 'base_url = "https://api.deepseek.com"' in body
    assert 'api_key_env = "DEEPSEEK_API_KEY"' in body


def test_the_retired_providers_are_not_offered(tmp_path):
    """`anthropic` and `command` still load from a hand-written specky.toml, but init writes
    only the agent or an OpenAI-compatible API."""
    for kind in ("anthropic", "command"):
        with pytest.raises(ConfigError, match="expected agent, openai-compatible or bedrock"):
            _config(tmp_path, provider=kind, **NO_VALIDATE)


def test_no_validate_makes_no_provider_call(tmp_path, monkeypatch):
    """The reason it exists: a snapshot build has no key in the environment yet, and shouldn't fail
    — or bill — for that."""
    monkeypatch.setattr(
        setup_wizard,
        "load_provider",
        lambda _config: pytest.fail("--no-validate still called the provider"),
    )

    assert 'provider = "agent"' in _config(tmp_path, assume_yes=True, validate=False)


def test_validation_happens_before_the_file_is_written(tmp_path, monkeypatch):
    """A provider that doesn't work must not leave a specky.toml claiming it does."""

    class Broken:
        def generate(self, _prompt, *, prefix: str = "", task: str = ""):
            raise RuntimeError("claude exited 1")

    monkeypatch.setattr(setup_wizard, "load_provider", lambda _config: Broken())

    with pytest.raises(RuntimeError):
        _config(tmp_path, assume_yes=True, validate=True)
    assert not (tmp_path / "specky.toml").exists()


# --- the docs root ------------------------------------------------------------------------


def test_docs_root_flag_writes_the_docs_table(tmp_path):
    body = _config(tmp_path, assume_yes=True, docs_root="documentation/", **NO_VALIDATE)

    assert "[docs]" in body
    assert 'root = "documentation"' in body, "trailing slash should be tidied away"


def test_the_default_docs_root_is_recorded_too(tmp_path):
    """No override, no collision — still spelled out, so specky.toml states which tree it's using
    rather than leaving the default to be known by heart."""
    body = _config(tmp_path, assume_yes=True, **NO_VALIDATE)

    assert 'root = "specs"' in body


@pytest.mark.parametrize("bad", ["/etc/specs", "../outside", "docs/../../outside"])
def test_a_docs_root_outside_the_repo_is_refused(tmp_path, bad):
    """Rejected rather than silently defaulted: an absolute path stripped of its slashes reads as an
    innocuous relative one."""
    with pytest.raises(ConfigError, match="inside the repo"):
        _config(tmp_path, assume_yes=True, docs_root=bad, **NO_VALIDATE)


def test_a_colliding_docs_root_is_reported_and_kept(tmp_path):
    """The question `_choose_docs_root` would have asked. Nobody here to answer, so it's reported
    with the flag that fixes it — refusing to write any config would be worse than a mixed tree."""
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "openapi.yaml").write_text("openapi: 3.1.0\n")
    printed: list[str] = []

    run_init(
        tmp_path / "specky.toml",
        input_fn=_never_asks,
        print_fn=printed.append,
        options=InitOptions(assume_yes=True, validate=False),
    )

    message = "\n".join(printed)
    assert "openapi.yaml" in message
    assert "--docs-root" in message
    assert 'root = "specs"' in (tmp_path / "specky.toml").read_text()


# --- re-running init ------------------------------------------------------------------------

_EXISTING = '''\
[ai]
provider = "openai-compatible"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"

[docs]
root = "specs"

# The viewer sits behind the team proxy.
[serve]
port = 9000
allow_origins = [
  "https://docs.example.com",
]

[skills]
model = "haiku"
'''


def test_rerunning_init_keeps_the_tables_it_doesnt_own(tmp_path):
    """init owns `[ai]` and `[docs]`. The viewer's port and the skills' model were set by someone
    else, and a provider switch mustn't quietly take them with it."""
    import tomllib

    path = tmp_path / "specky.toml"
    path.write_text(_EXISTING)
    printed: list[str] = []

    run_init(
        path,
        input_fn=_never_asks,
        print_fn=printed.append,
        options=InitOptions(assume_yes=True, **NO_VALIDATE),
    )

    body = path.read_text()
    before, after = tomllib.loads(_EXISTING), tomllib.loads(body)
    assert after["ai"]["provider"] == "agent"
    assert "base_url" not in after["ai"], "the old provider's fields outlived it"
    assert (after["serve"], after["skills"]) == (before["serve"], before["skills"])
    assert "# The viewer sits behind the team proxy.\n[serve]" in body
    assert any("[serve], [skills]" in line for line in printed)


def test_an_unparseable_specky_toml_stops_init_before_it_asks_or_bills(tmp_path, monkeypatch):
    """With no parse there's no telling which tables to keep, and overwriting would lose them."""
    monkeypatch.setattr(
        setup_wizard, "load_provider", lambda _config: pytest.fail("called the provider anyway")
    )
    path = tmp_path / "specky.toml"
    path.write_text("[serve\nport = 9000\n")

    with pytest.raises(ConfigError, match="isn't valid TOML"):
        run_init(path, input_fn=_never_asks, print_fn=lambda _msg: None)

    assert path.read_text() == "[serve\nport = 9000\n"


def test_a_table_init_would_mangle_stops_the_write(tmp_path):
    """The splitter reads a `[`-led line as a header, even inside a multi-line string. The merge is
    compared with the original before it's written, so that misread is an error, not a lost table."""
    path = tmp_path / "specky.toml"
    original = '[ai]\nprovider = "anthropic"\n\n[serve]\nbanner = """\n[ai]\n"""\n'
    path.write_text(original)

    with pytest.raises(ConfigError, match="rest of specky.toml"):
        _config(tmp_path, assume_yes=True, **NO_VALIDATE)

    assert path.read_text() == original


# --- the interview still works -------------------------------------------------------------


def _interview(tmp_path, monkeypatch, answers):
    monkeypatch.setattr(setup_wizard, "load_provider", lambda _config: _OkProvider())
    replies = iter(answers)
    printed: list[str] = []
    run_init(tmp_path / "specky.toml", input_fn=lambda _p: next(replies), print_fn=printed.append)
    return (tmp_path / "specky.toml").read_text(), printed


def test_the_interview_defaults_to_the_current_agent(tmp_path, monkeypatch):
    """Enter, Enter: the agent on its own default model — no key to set up."""
    body, _ = _interview(tmp_path, monkeypatch, ["", ""])

    assert 'provider = "agent"' in body
    assert 'agent = "claude"' in body
    assert "model" not in body


def test_the_interview_takes_an_explicit_model(tmp_path, monkeypatch):
    body, _ = _interview(tmp_path, monkeypatch, ["y", "opus"])
    assert 'model = "opus"' in body


def _openai_answers():
    return ["", "https://api.deepseek.com", "deepseek-chat", "DEEPSEEK_API_KEY"]


def test_declining_the_agent_asks_for_an_openai_compatible_api(tmp_path, monkeypatch):
    body, _ = _interview(tmp_path, monkeypatch, ["n", *_openai_answers()])

    assert 'provider = "openai-compatible"' in body
    assert 'model = "deepseek-chat"' in body


def test_without_an_agent_the_interview_goes_straight_to_the_api(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "current_agent", lambda: None)
    body, printed = _interview(tmp_path, monkeypatch, _openai_answers())

    assert 'provider = "openai-compatible"' in body
    assert any("No coding agent found" in line for line in printed)


def test_the_interview_can_choose_bedrock(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    body, _ = _interview(tmp_path, monkeypatch, ["n", "2", "", "eu-west-1", ""])

    assert 'provider = "bedrock"' in body
    assert f'model = "{setup_wizard.DEFAULT_BEDROCK_MODEL}"' in body
    assert 'aws_region = "eu-west-1"' in body
    assert "aws_profile" not in body


def test_bedrock_by_flag_leaves_region_to_the_aws_chain(tmp_path):
    """No region or profile named means none written — AWS_REGION or an instance role decides."""
    body = _config(tmp_path, provider="bedrock", **NO_VALIDATE)

    assert 'provider = "bedrock"' in body
    assert f'model = "{setup_wizard.DEFAULT_BEDROCK_MODEL}"' in body
    assert "aws_region" not in body


def test_bedrock_flags_are_written(tmp_path):
    body = _config(
        tmp_path,
        provider="bedrock",
        model="anthropic.claude-sonnet-5",
        aws_region="us-east-1",
        aws_profile="docs",
        **NO_VALIDATE,
    )
    assert 'model = "anthropic.claude-sonnet-5"' in body
    assert 'aws_region = "us-east-1"' in body
    assert 'aws_profile = "docs"' in body


def test_an_agent_can_be_named_by_flag(tmp_path):
    body = _config(tmp_path, provider="agent", agent="opencode", **NO_VALIDATE)
    assert 'agent = "opencode"' in body


def test_an_unknown_agent_is_refused_before_the_write(tmp_path):
    with pytest.raises(ConfigError, match="isn't one specky knows"):
        _config(tmp_path, provider="agent", agent="hal9000", **NO_VALIDATE)
    assert not (tmp_path / "specky.toml").exists()


class _OkProvider:
    def generate(self, _prompt, *, prefix: str = "", task: str = ""):
        return "ok"


# --- the CLI layer -----------------------------------------------------------------------


def test_init_without_a_terminal_says_so(tmp_repo, monkeypatch):
    """The failure this whole flag set exists to prevent: `input()` on a closed stdin raises
    EOFError a question or two in, after some answers have been given and before anything is
    written. The message has to name the flags instead."""
    monkeypatch.chdir(tmp_repo)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    args = cli.build_parser().parse_args(["init"])
    with pytest.raises(ValueError, match="not a terminal"):
        args.func(args)

    assert not (tmp_repo / "specky.toml").exists()


def test_init_flags_reach_the_wizard(tmp_repo, monkeypatch):
    """The parser's names and `InitOptions`' fields drift apart silently otherwise — `--command`
    """
    monkeypatch.chdir(tmp_repo)
    captured: dict = {}
    # The handler imports `run_init` inside its body, so patching the module's attribute is enough.
    monkeypatch.setattr(
        setup_wizard,
        "run_init",
        lambda path, options=None, **_kw: captured.update(path=path, options=options),
    )

    args = cli.build_parser().parse_args(
        [
            "init",
            "--provider",
            "agent",
            "--agent",
            "codex",
            "--model",
            "gpt-5",
            "--docs-root",
            "documentation",
            "--no-validate",
        ]
    )
    args.func(args)

    assert captured["path"] == tmp_repo / "specky.toml"
    options = captured["options"]
    assert (options.provider, options.agent, options.model) == ("agent", "codex", "gpt-5")
    assert (options.docs_root, options.validate) == ("documentation", False)


def test_every_init_flag_is_a_known_flag():
    """`generator.ungrounded_flags` checks generated docs against `cli.known_flags()`, so a flag the
    parser gained has to be visible there or a doc mentioning it reads as a hallucination."""
    assert {"--yes", "--provider", "--api-key-env", "--docs-root", "--no-validate"} <= cli.known_flags()


# --- .gitignore ------------------------------------------------------------------------------


def _init_in(repo) -> list[str]:
    printed: list[str] = []
    run_init(
        repo / "specky.toml",
        input_fn=_never_asks,
        print_fn=printed.append,
        options=InitOptions(assume_yes=True, **NO_VALIDATE),
    )
    return printed


def test_init_gitignores_specky_toml(tmp_repo):
    """A repo adopting specky has no rule for specky.toml yet, and it's per-machine config: the
    first `git add -A` after init would otherwise commit one developer's provider for everyone."""
    from conftest import git

    printed = _init_in(tmp_repo)

    assert (tmp_repo / ".gitignore").read_text() == "specky.toml\n"
    assert any(".gitignore" in line for line in printed)
    assert "specky.toml" not in git(tmp_repo, "status", "--porcelain")


def test_init_appends_to_an_existing_gitignore_once(tmp_repo):
    (tmp_repo / ".gitignore").write_text("node_modules/")  # no trailing newline

    _init_in(tmp_repo)
    _init_in(tmp_repo)  # re-running init must not add a second line

    assert (tmp_repo / ".gitignore").read_text() == "node_modules/\nspecky.toml\n"


def test_init_leaves_gitignore_alone_when_already_ignored(tmp_repo):
    (tmp_repo / ".gitignore").write_text("*.toml\n")

    printed = _init_in(tmp_repo)

    assert (tmp_repo / ".gitignore").read_text() == "*.toml\n"
    assert not any(".gitignore" in line for line in printed)


def test_init_respects_a_committed_specky_toml(tmp_repo):
    """Someone chose to commit it; a `.gitignore` line wouldn't untrack it, only confuse."""
    from conftest import git

    (tmp_repo / "specky.toml").write_text("[ai]\n")
    git(tmp_repo, "add", "specky.toml")
    git(tmp_repo, "commit", "-q", "-m", "commit the config")

    _init_in(tmp_repo)

    assert not (tmp_repo / ".gitignore").exists()
