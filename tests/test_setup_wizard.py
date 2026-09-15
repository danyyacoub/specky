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


def _config(tmp_path, **options):
    """Run a non-interactive init and hand back the specky.toml it wrote."""
    path = tmp_path / "specky.toml"
    run_init(path, input_fn=_never_asks, print_fn=lambda _msg: None, options=InitOptions(**options))
    return path.read_text()


def _never_asks(prompt: str) -> str:
    raise AssertionError(f"non-interactive init asked a question: {prompt!r}")


# --- the flags ---------------------------------------------------------------------------


def test_yes_alone_writes_the_default_anthropic_config(tmp_path):
    """`specky init --yes` is the one-liner a setup script wants: the same answers the first
    prompt's default gives, with nothing to type."""
    body = _config(tmp_path, assume_yes=True, **NO_VALIDATE)

    assert '[ai]' in body
    assert 'provider = "anthropic"' in body
    assert f'model = "{setup_wizard.DEFAULT_MODEL}"' in body
    assert f'api_key_env = "{setup_wizard.DEFAULT_API_KEY_ENV}"' in body


def test_naming_a_provider_is_enough_to_skip_the_interview(tmp_path):
    """`--provider` implies `--yes`: the interview's whole job is finding out which provider."""
    assert InitOptions(provider="anthropic").non_interactive
    body = _config(tmp_path, provider="anthropic", model="claude-opus-5", **NO_VALIDATE)

    assert 'model = "claude-opus-5"' in body


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


def test_a_command_provider_needs_a_command(tmp_path):
    with pytest.raises(ConfigError, match="--command"):
        _config(tmp_path, provider="command", **NO_VALIDATE)


def test_no_validate_makes_no_provider_call(tmp_path, monkeypatch):
    """The reason it exists: a snapshot build has no key in the environment yet, and shouldn't fail
    — or bill — for that."""
    monkeypatch.setattr(
        setup_wizard,
        "load_provider",
        lambda _config: pytest.fail("--no-validate still called the provider"),
    )

    assert 'provider = "anthropic"' in _config(tmp_path, assume_yes=True, validate=False)


def test_validation_happens_before_the_file_is_written(tmp_path, monkeypatch):
    """A provider that doesn't work must not leave a specky.toml claiming it does."""

    class Broken:
        def generate(self, _prompt):
            raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")

    monkeypatch.setattr(setup_wizard, "load_provider", lambda _config: Broken())

    with pytest.raises(RuntimeError):
        _config(tmp_path, assume_yes=True, validate=True)
    assert not (tmp_path / "specky.toml").exists()


# --- the docs root ------------------------------------------------------------------------


def test_docs_root_flag_writes_the_docs_table(tmp_path):
    body = _config(tmp_path, assume_yes=True, docs_root="documentation/", **NO_VALIDATE)

    assert "[docs]" in body
    assert 'root = "documentation"' in body, "trailing slash should be tidied away"


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
    assert "[docs]" not in (tmp_path / "specky.toml").read_text()


# --- the interview still works -------------------------------------------------------------


def test_the_interview_is_still_the_default(tmp_path, monkeypatch):
    """No options at all means ask, which is what a person at a terminal gets."""
    monkeypatch.setattr(setup_wizard, "load_provider", lambda _config: _OkProvider())
    answers = iter(["n", "anthropic", "claude-sonnet-5", "MY_KEY"])

    run_init(
        tmp_path / "specky.toml",
        input_fn=lambda _prompt: next(answers),
        print_fn=lambda _msg: None,
    )

    body = (tmp_path / "specky.toml").read_text()
    assert 'model = "claude-sonnet-5"' in body
    assert 'api_key_env = "MY_KEY"' in body


class _OkProvider:
    def generate(self, _prompt):
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
    lands on `provider_command` to keep it off argparse's own `command` dest."""
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
            "command",
            "--command",
            "claude -p",
            "--docs-root",
            "documentation",
            "--no-validate",
        ]
    )
    args.func(args)

    assert captured["path"] == tmp_repo / "specky.toml"
    options = captured["options"]
    assert (options.provider, options.command) == ("command", "claude -p")
    assert (options.docs_root, options.validate) == ("documentation", False)


def test_every_init_flag_is_a_known_flag():
    """`generator.ungrounded_flags` checks generated docs against `cli.known_flags()`, so a flag the
    parser gained has to be visible there or a doc mentioning it reads as a hallucination."""
    assert {"--yes", "--provider", "--api-key-env", "--docs-root", "--no-validate"} <= cli.known_flags()
