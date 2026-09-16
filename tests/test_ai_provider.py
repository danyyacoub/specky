import pytest

from specky import ai_provider
from specky.ai_provider import (
    DEFAULT_MAX_TOKENS,
    AnthropicProvider,
    CachingProvider,
    CommandProvider,
    ConfigError,
    OpenAICompatibleProvider,
    load_provider,
    load_provider_from_toml,
    unwrap,
)


def test_anthropic_defaults():
    provider = load_provider({"provider": "anthropic"})
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-haiku-4-5"
    assert provider.api_key_env == "ANTHROPIC_API_KEY"
    assert provider.max_tokens == DEFAULT_MAX_TOKENS


def test_max_tokens_is_configurable():
    provider = load_provider({"provider": "anthropic", "max_tokens": 8192})
    assert provider.max_tokens == 8192


def test_max_tokens_accepts_a_quoted_value():
    """`specky init` writes every [ai] value as a quoted string, so a string must work."""
    provider = load_provider({"provider": "anthropic", "max_tokens": "2048"})
    assert provider.max_tokens == 2048


@pytest.mark.parametrize("bad", ["lots", "", 0, -1, None])
def test_bad_max_tokens_is_a_config_error(bad):
    with pytest.raises(ConfigError):
        load_provider({"provider": "anthropic", "max_tokens": bad})


def test_openai_compatible_requires_its_fields():
    with pytest.raises(ConfigError, match="base_url"):
        load_provider({"provider": "openai-compatible", "model": "m", "api_key_env": "K"})


def test_openai_compatible_carries_max_tokens():
    provider = load_provider(
        {
            "provider": "openai-compatible",
            "base_url": "https://api.example.com/v1/",
            "model": "m",
            "api_key_env": "K",
            "max_tokens": 512,
        }
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.max_tokens == 512


def test_command_provider_requires_a_command():
    with pytest.raises(ConfigError, match="command"):
        load_provider({"provider": "command"})


def test_unknown_provider():
    with pytest.raises(ConfigError, match="Unknown"):
        load_provider({"provider": "telepathy"})


def test_command_provider_round_trip():
    provider = CommandProvider(command="cat")
    assert provider.generate("hello\n") == "hello"


def test_load_from_toml_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_provider_from_toml(tmp_path / "specky.toml")


def test_load_from_toml_without_ai_table(tmp_path):
    path = tmp_path / "specky.toml"
    path.write_text("[other]\nkey = 1\n")
    with pytest.raises(ConfigError, match="no \\[ai\\] table"):
        load_provider_from_toml(path)


def test_load_from_toml(tmp_path):
    """Caching by default, so every caller gets it without asking — `load_provider_from_toml` is
    the only construction path, which is what makes that possible."""
    path = tmp_path / "specky.toml"
    path.write_text('[ai]\nprovider = "command"\ncommand = "cat"\n')
    provider = load_provider_from_toml(path)
    assert isinstance(provider, CachingProvider)
    assert isinstance(unwrap(provider), CommandProvider)


def test_the_cache_can_be_turned_off(tmp_path):
    path = tmp_path / "specky.toml"
    path.write_text('[ai]\nprovider = "command"\ncommand = "cat"\ncache = false\n')
    assert isinstance(load_provider_from_toml(path), CommandProvider)


def test_a_quoted_cache_flag_is_honoured(tmp_path):
    """`specky init` writes every [ai] value quoted, so "false" has to mean false."""
    path = tmp_path / "specky.toml"
    path.write_text('[ai]\nprovider = "command"\ncommand = "cat"\ncache = "false"\n')
    assert isinstance(load_provider_from_toml(path), CommandProvider)


class _FakeAnthropicResponse:
    def __init__(self, stop_reason: str) -> None:
        self.stop_reason = stop_reason
        self.content = [type("Block", (), {"text": "generated doc"})()]


def _install_fake_anthropic(monkeypatch, stop_reason: str) -> dict:
    """Stand in for the `anthropic` module, capturing the kwargs sent to messages.create."""
    seen: dict = {}

    class Messages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return _FakeAnthropicResponse(stop_reason)

    class Client:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs
            self.messages = Messages()

    module = type("anthropic", (), {"Anthropic": Client})
    monkeypatch.setitem(__import__("sys").modules, "anthropic", module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return seen


def test_anthropic_sends_configured_max_tokens(monkeypatch):
    seen = _install_fake_anthropic(monkeypatch, stop_reason="end_turn")
    assert AnthropicProvider(max_tokens=1234).generate("prompt") == "generated doc"
    assert seen["max_tokens"] == 1234
    assert seen["client_kwargs"]["timeout"] == ai_provider.DEFAULT_TIMEOUT_SECONDS


def test_anthropic_refuses_a_truncated_response(monkeypatch):
    """A doc cut off at the token limit must not be returned — it would be written to specs/
    as if it were complete."""
    _install_fake_anthropic(monkeypatch, stop_reason="max_tokens")
    with pytest.raises(ai_provider.TruncatedResponse, match="max_tokens"):
        AnthropicProvider().generate("prompt")


def test_anthropic_requires_the_api_key_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider().generate("prompt")


# --- the cacheable prefix -------------------------------------------------------------------------


class _Recorder:
    """Records how a call was split, without pretending to be any real provider."""

    def __init__(self, reply: str = "ok") -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._reply = reply

    def generate(self, prompt: str, *, prefix: str = "", task: str = "") -> str:
        self.calls.append((prefix, prompt, task))
        return self._reply


def test_anthropic_puts_the_prefix_in_a_cached_system_block(monkeypatch):
    sent = {}

    class FakeMessages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return type(
                "R", (), {"stop_reason": "end_turn", "content": [type("T", (), {"text": "ok"})()]}
            )()

    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        type(
            "m",
            (),
            {"Anthropic": staticmethod(lambda **kw: type("C", (), {"messages": FakeMessages()})())},
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    AnthropicProvider().generate("the volatile half", prefix="the stable half")

    assert sent["system"] == [
        {"type": "text", "text": "the stable half", "cache_control": {"type": "ephemeral"}}
    ]
    assert sent["messages"] == [{"role": "user", "content": "the volatile half"}]


def test_no_prefix_sends_no_system_block(monkeypatch):
    sent = {}

    class FakeMessages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return type(
                "R", (), {"stop_reason": "end_turn", "content": [type("T", (), {"text": "ok"})()]}
            )()

    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        type("m", (), {"Anthropic": staticmethod(lambda **kw: type("C", (), {"messages": FakeMessages()})())}),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    AnthropicProvider().generate("just this")

    assert "system" not in sent


def test_a_command_provider_concatenates_the_two_halves(tmp_path):
    """An arbitrary CLI has one input channel, so the split has to be undone before it runs."""
    script = tmp_path / "echo.sh"
    script.write_text("#!/bin/sh\ncat\n")
    script.chmod(0o755)

    out = CommandProvider(command=str(script)).generate("second", prefix="first")

    assert out == "first\n\nsecond"


def test_the_cache_key_covers_the_prefix(tmp_repo):
    """Two calls differing only in their stable half are two different questions, so the memoized
    answer to one must not be served for the other."""
    inner = _Recorder()
    provider = CachingProvider(inner, tmp_repo, "m", "test")

    provider.generate("same", prefix="one")
    provider.generate("same", prefix="two")

    assert len(inner.calls) == 2, "the second call must not have been served from cache"


def test_an_identical_split_call_is_served_from_cache(tmp_repo):
    inner = _Recorder()
    provider = CachingProvider(inner, tmp_repo, "m", "test")

    provider.generate("same", prefix="one")
    provider.generate("same", prefix="one")

    assert len(inner.calls) == 1


# --- per-task models ------------------------------------------------------------------------------


def test_a_task_model_routes_only_that_task(tmp_path):
    (tmp_path / "specky.toml").write_text(
        '[ai]\nprovider = "anthropic"\nmodel = "haiku"\ndiscovery_model = "sonnet"\ncache = false\n'
    )
    provider = load_provider_from_toml(tmp_path / "specky.toml")

    assert provider.for_task("discovery").model == "sonnet"
    assert provider.for_task("classify").model == "haiku"
    assert provider.for_task("").model == "haiku"


def test_no_task_models_returns_a_plain_provider(tmp_path):
    (tmp_path / "specky.toml").write_text('[ai]\nprovider = "anthropic"\ncache = false\n')
    provider = load_provider_from_toml(tmp_path / "specky.toml")

    assert isinstance(provider, AnthropicProvider)


def test_a_misspelled_task_model_is_a_config_error(tmp_path):
    (tmp_path / "specky.toml").write_text('[ai]\nprovider = "anthropic"\ndocs_model = "x"\n')
    with pytest.raises(ConfigError, match="names no task"):
        load_provider_from_toml(tmp_path / "specky.toml")


def test_a_command_provider_cannot_have_task_models(tmp_path):
    """There is no model to swap, so a per-task model there would look configured and do nothing."""
    (tmp_path / "specky.toml").write_text(
        '[ai]\nprovider = "command"\ncommand = "llm"\ndoc_model = "x"\n'
    )
    with pytest.raises(ConfigError, match="command"):
        load_provider_from_toml(tmp_path / "specky.toml")


# --- batch ------------------------------------------------------------------------------------------


def test_supports_batch_is_false_for_providers_without_one():
    assert not ai_provider.supports_batch(CommandProvider(command="x"))
    assert not ai_provider.supports_batch(
        OpenAICompatibleProvider(base_url="u", model="m", api_key_env="K")
    )
    assert ai_provider.supports_batch(AnthropicProvider())


def test_a_batch_serves_cache_hits_and_only_sends_the_misses(tmp_repo):
    class Batcher(_Recorder):
        def generate_batch(self, prompts, task: str = ""):
            self.batched = prompts
            return {key: f"answer for {key}" for key in prompts}

    inner = Batcher()
    provider = CachingProvider(inner, tmp_repo, "m", "test")
    provider.generate("b-prompt", prefix="p")  # memoize one of the two

    answers = provider.generate_batch({"a": ("p", "a-prompt"), "b": ("p", "b-prompt")})

    assert set(answers) == {"a", "b"}
    assert set(inner.batched) == {"a"}, "the already-cached entry must not be re-sent"
    assert answers["b"] == "ok", "and its memoized answer is the one returned"


def test_batch_ids_survive_a_key_with_a_slash():
    """Bootstrap keys batch entries by `domain/topic`, which the Batches API's custom_id rejects."""
    ident = ai_provider._batch_id("billing/refund-flow")
    assert ident.isalnum() and len(ident) <= 64
