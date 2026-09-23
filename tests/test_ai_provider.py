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


def test_an_agent_provider_is_the_agents_headless_command():
    """`provider = "agent"` is `command` with the command line written for it."""
    provider = load_provider({"provider": "agent", "agent": "claude", "model": "opus"})
    assert isinstance(provider, CommandProvider)
    assert provider.command == "claude -p --model opus"


def test_an_agent_without_a_model_keeps_its_own_default():
    provider = load_provider({"provider": "agent", "agent": "claude"})
    assert provider.command == "claude -p"


def test_the_stdin_marker_stays_last():
    """`codex exec -` reads stdin only when `-` is the final argument."""
    provider = load_provider({"provider": "agent", "agent": "codex", "model": "gpt-5"})
    assert provider.command.endswith("--model gpt-5 -")


def test_an_unknown_agent_is_a_config_error():
    with pytest.raises(ConfigError, match="isn't one specky knows"):
        load_provider({"provider": "agent", "agent": "hal9000"})
    with pytest.raises(ConfigError, match="requires 'agent'"):
        load_provider({"provider": "agent"})


def test_an_agent_provider_can_route_task_models(tmp_path):
    """Unlike a hand-written command, an agent's model is a real setting, so it can be swapped."""
    (tmp_path / "specky.toml").write_text(
        '[ai]\nprovider = "agent"\nagent = "claude"\nmodel = "haiku"\n'
        'document_model = "opus"\ncache = false\n'
    )
    router = load_provider_from_toml(tmp_path / "specky.toml")
    assert unwrap(router).command == "claude -p --model haiku"
    assert router.for_task("document").command == "claude -p --model opus"


def test_the_current_agent_is_the_one_whose_session_this_is(monkeypatch):
    monkeypatch.setattr(ai_provider.shutil, "which", lambda exe: f"/bin/{exe}")
    assert ai_provider.current_agent({}) == "claude", "none running: first installed"
    assert ai_provider.current_agent({"OPENCODE": "1"}) == "opencode"
    assert ai_provider.current_agent({"AI_AGENT": "gemini-cli_0-30_agent"}) == "gemini"


def test_a_running_agent_that_isnt_on_path_is_skipped(monkeypatch):
    monkeypatch.setattr(
        ai_provider.shutil, "which", lambda exe: "/bin/kiro-cli" if exe == "kiro-cli" else None
    )
    assert ai_provider.current_agent({"CLAUDECODE": "1"}) == "kiro"
    monkeypatch.setattr(ai_provider.shutil, "which", lambda _exe: None)
    assert ai_provider.current_agent({"CLAUDECODE": "1"}) is None


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
        '[ai]\nprovider = "anthropic"\nmodel = "haiku"\ndocument_model = "sonnet"\ncache = false\n'
    )
    provider = load_provider_from_toml(tmp_path / "specky.toml")

    assert provider.for_task("document").model == "sonnet"
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


# --- the tool loop --------------------------------------------------------------------------------
#
# The two `converse` implementations are the only code in specky that holds a conversation, and
# almost all of what can go wrong in them is a message shape: a tool result threaded back under the
# wrong key, an assistant turn dropped, a `stop_reason` misread. None of that surfaces as an
# exception — it surfaces as a model that cannot see its own tool results. So these fakes assert on
# what was sent, turn by turn, rather than only on what came back.


class _Tool:
    """The duck type `converse` reads: a name, a description, a schema. `run` is never called
    through the provider — `invoke` is the only way a tool is reached."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"does {name}"
        self.schema = {"type": "object", "properties": {}}


def _anthropic_turns(monkeypatch, replies):
    """Fake the SDK with a scripted list of responses, recording every request.

    `messages` is snapshotted, not referenced: the loop appends to one list across every turn, so
    recording the list itself would show each turn the conversation's final state.
    """
    sent: list[dict] = []

    class Messages:
        def create(self, **kwargs):
            sent.append({**kwargs, "messages": list(kwargs["messages"])})
            return replies[len(sent) - 1]

    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        type(
            "m",
            (),
            {"Anthropic": staticmethod(lambda **kw: type("C", (), {"messages": Messages()})())},
        ),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    return sent


def _text_block(text: str):
    return type("T", (), {"type": "text", "text": text})()


def _tool_block(name: str, arguments: dict, block_id: str = "tu_1"):
    return type("U", (), {"type": "tool_use", "id": block_id, "name": name, "input": arguments})()


def _reply(stop_reason: str, *blocks):
    return type("R", (), {"stop_reason": stop_reason, "content": list(blocks)})()


def test_anthropic_threads_a_tool_result_back_into_the_conversation(monkeypatch):
    sent = _anthropic_turns(
        monkeypatch,
        [
            _reply("tool_use", _tool_block("search_code", {"query": "refund"})),
            _reply("end_turn", _text_block("done")),
        ],
    )
    calls = []

    def invoke(name, arguments):
        calls.append((name, arguments))
        return "src/billing/refund.py:1: def refund_order():"

    said = AnthropicProvider().converse(
        "find it", prefix="rules", tools=[_Tool("search_code")], invoke=invoke
    )

    assert said == "done"
    assert calls == [("search_code", {"query": "refund"})]
    # Turn two must carry: the original ask, the assistant's tool_use, and the result under the id
    # it was asked for. A result the model can't match to its own call is a result it can't read.
    second = sent[1]["messages"]
    assert second[0] == {"role": "user", "content": "find it"}
    assert second[1]["role"] == "assistant"
    assert second[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": "src/billing/refund.py:1: def refund_order():",
            }
        ],
    }


def test_anthropic_sends_the_tools_and_keeps_caching_the_prefix(monkeypatch):
    """The prefix is resent on every turn, so losing its cache breakpoint costs the run over."""
    sent = _anthropic_turns(
        monkeypatch,
        [
            _reply("tool_use", _tool_block("read_file", {"path": "a.py"})),
            _reply("end_turn", _text_block("ok")),
        ],
    )

    AnthropicProvider().converse(
        "go", prefix="the stable half", tools=[_Tool("read_file")], invoke=lambda n, a: "text"
    )

    for request in sent:
        assert request["system"] == [
            {"type": "text", "text": "the stable half", "cache_control": {"type": "ephemeral"}}
        ]
        assert request["tools"] == [
            {
                "name": "read_file",
                "description": "does read_file",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]


def test_anthropic_runs_every_tool_call_in_one_turn(monkeypatch):
    """A model may ask for several files at once, and answering only the first strands the rest."""
    _anthropic_turns(
        monkeypatch,
        [
            _reply(
                "tool_use",
                _tool_block("read_file", {"path": "a.py"}, "tu_1"),
                _tool_block("read_file", {"path": "b.py"}, "tu_2"),
            ),
            _reply("end_turn", _text_block("ok")),
        ],
    )
    calls = []

    AnthropicProvider().converse(
        "go",
        tools=[_Tool("read_file")],
        invoke=lambda name, arguments: calls.append(arguments["path"]) or "text",
    )

    assert calls == ["a.py", "b.py"]


def test_anthropic_forces_the_terminal_tool_on_the_last_turn(monkeypatch):
    """Otherwise a model still exploring at turn twelve ends the run with nothing to show for
    twelve billed calls."""
    sent = _anthropic_turns(
        monkeypatch,
        [_reply("tool_use", _tool_block("read_file", {"path": "a.py"}))] * 3,
    )

    AnthropicProvider().converse(
        "go",
        tools=[_Tool("read_file"), _Tool("submit_doc")],
        invoke=lambda n, a: "text",
        max_turns=3,
        final_tool="submit_doc",
    )

    assert "tool_choice" not in sent[0]
    assert "tool_choice" not in sent[1]
    assert sent[2]["tool_choice"] == {"type": "tool", "name": "submit_doc"}


def test_a_terminal_tool_unwinds_straight_past_the_loop(monkeypatch):
    """The provider must not catch it: that raise is how a run ends successfully, and swallowing it
    would turn a finished doc into 'the model stopped without submitting'."""

    class Done(Exception):
        pass

    _anthropic_turns(monkeypatch, [_reply("tool_use", _tool_block("submit_doc", {}))])

    def invoke(name, arguments):
        raise Done()

    with pytest.raises(Done):
        AnthropicProvider().converse("go", tools=[_Tool("submit_doc")], invoke=invoke)


def test_anthropic_refuses_a_truncated_turn(monkeypatch):
    _anthropic_turns(monkeypatch, [_reply("max_tokens", _text_block("half a d"))])

    with pytest.raises(ai_provider.TruncatedResponse, match="max_tokens"):
        AnthropicProvider().converse("go", tools=[_Tool("read_file")], invoke=lambda n, a: "")


def test_converse_reports_each_turn_for_the_usage_log(monkeypatch):
    """One `specky document` run is many billed calls, and `specky cost` should say so."""
    _anthropic_turns(
        monkeypatch,
        [
            _reply("tool_use", _tool_block("read_file", {"path": "a.py"})),
            _reply("end_turn", _text_block("done")),
        ],
    )
    turns = []

    AnthropicProvider().converse(
        "go",
        prefix="rules",
        tools=[_Tool("read_file")],
        invoke=lambda n, a: "text",
        on_turn=lambda sent, received: turns.append((sent, received)),
    )

    assert len(turns) == 2
    assert turns[1][0] > turns[0][0], "the second turn carries the first turn's result too"


def test_a_command_provider_cannot_hold_a_conversation():
    """One stdin, one stdout, nowhere to put a tool definition — a normal outcome, not a failure."""
    provider = CommandProvider(command="claude -p")

    assert ai_provider.supports_tools(provider) is False
    with pytest.raises(ai_provider.ToolLoopUnsupported, match="no tool channel"):
        provider.converse("go", tools=[], invoke=lambda n, a: "")


def test_the_caching_provider_never_memoizes_a_conversation(tmp_repo):
    """The key is model + prefix + prompt, which cannot see which files the model chose to read —
    so a hit after a code change would serve a doc describing the repo as it used to be."""

    class Counting:
        def __init__(self):
            self.calls = 0

        def converse(self, prompt, **kwargs):
            self.calls += 1
            return "answer"

    inner = Counting()
    provider = CachingProvider(inner, tmp_repo, "model", "document")

    assert provider.converse("same", prefix="same", tools=[], invoke=lambda n, a: "") == "answer"
    assert provider.converse("same", prefix="same", tools=[], invoke=lambda n, a: "") == "answer"
    assert inner.calls == 2, "an identical request must still be asked again"


def test_the_caching_provider_logs_one_usage_row_per_turn(tmp_repo):
    from specky.db import connect

    class TwoTurns:
        def converse(self, prompt, *, on_turn=None, **kwargs):
            on_turn(100, 10)
            on_turn(250, 20)
            return "answer"

    CachingProvider(TwoTurns(), tmp_repo, "model", "document").converse(
        "go", tools=[], invoke=lambda n, a: ""
    )

    conn = connect(tmp_repo)
    try:
        rows = conn.execute(
            "SELECT prompt_chars, response_chars, cached FROM usage WHERE command = 'document'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(100, 10, 0), (250, 20, 0)]


# --- the OpenAI-compatible loop ---------------------------------------------------------------------


def _openai_turns(monkeypatch, payloads):
    """Fake httpx.post with a scripted list of chat-completion bodies, recording every request."""
    sent: list[dict] = []

    class Response:
        def __init__(self, body):
            self._body = body
            self.status_code = 200
            self.text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    def post(url, **kwargs):
        body = kwargs["json"]
        sent.append({**body, "messages": list(body["messages"])})  # snapshot, see _anthropic_turns
        return Response(payloads[len(sent) - 1])

    monkeypatch.setattr(__import__("httpx"), "post", post)
    monkeypatch.setenv("KEY", "x")
    return sent


def _openai_provider():
    return OpenAICompatibleProvider(base_url="http://x/v1", model="m", api_key_env="KEY")


def test_openai_threads_a_tool_result_back_under_its_call_id(monkeypatch):
    call = {"id": "call_1", "function": {"name": "search_code", "arguments": '{"query": "refund"}'}}
    sent = _openai_turns(
        monkeypatch,
        [
            {"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ],
    )
    calls = []

    said = _openai_provider().converse(
        "find it",
        prefix="rules",
        tools=[_Tool("search_code")],
        invoke=lambda name, arguments: calls.append((name, arguments)) or "a hit",
    )

    assert said == "done"
    assert calls == [("search_code", {"query": "refund"})]
    assert sent[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "a hit",
    }
    assert sent[0]["tools"][0]["function"]["name"] == "search_code"
    assert sent[0]["messages"][0] == {"role": "system", "content": "rules"}


def test_openai_arguments_that_are_not_json_come_back_as_the_tool_result(monkeypatch):
    """The model wrote them, it can see they didn't parse, and the next turn usually fixes it."""
    call = {"id": "call_1", "function": {"name": "search_code", "arguments": "{oops"}}
    sent = _openai_turns(
        monkeypatch,
        [
            {"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "recovered"}}]},
        ],
    )

    said = _openai_provider().converse(
        "go",
        tools=[_Tool("search_code")],
        invoke=lambda n, a: pytest.fail("the tool must not run on unparseable arguments"),
    )

    assert said == "recovered"
    assert "not valid JSON" in sent[1]["messages"][-1]["content"]


def test_an_endpoint_that_rejects_tools_names_the_lever(monkeypatch):
    """"OpenAI-compatible" is a claim about the chat shape, and tool calling is the part endpoints
    most often leave out — an httpx stack trace would read like a specky bug."""

    class Response:
        status_code = 400
        text = "this model does not support tools"

    monkeypatch.setattr(__import__("httpx"), "post", lambda url, **kw: Response())
    monkeypatch.setenv("KEY", "x")

    with pytest.raises(ConfigError, match="tool-calling"):
        _openai_provider().converse("go", tools=[_Tool("x")], invoke=lambda n, a: "")


def test_openai_refuses_a_truncated_turn(monkeypatch):
    _openai_turns(
        monkeypatch, [{"choices": [{"finish_reason": "length", "message": {"content": "half"}}]}]
    )

    with pytest.raises(ai_provider.TruncatedResponse, match="max_tokens"):
        _openai_provider().converse("go", tools=[_Tool("x")], invoke=lambda n, a: "")


# --- being made to finish -------------------------------------------------------------------------
#
# The failure these cover was observed against a real endpoint: after eleven useful tool calls the
# model wrote the finished document as prose in its reply rather than calling `submit_doc`, and the
# run was discarded. Forcing the tool only on the *last* turn never applied, because the model
# stopped talking before reaching it.


def test_a_model_that_answers_in_prose_is_nudged_and_recovered(monkeypatch):
    sent = _anthropic_turns(
        monkeypatch,
        [
            _reply("end_turn", _text_block("# The Doc\n\nHere it is, as text.")),
            _reply("tool_use", _tool_block("submit_doc", {"markdown": "# The Doc"})),
        ],
    )
    submitted = []

    with pytest.raises(RuntimeError):

        def invoke(name, arguments):
            submitted.append((name, arguments))
            raise RuntimeError("terminal")

        AnthropicProvider().converse(
            "go",
            tools=[_Tool("read_file"), _Tool("submit_doc")],
            invoke=invoke,
            max_turns=8,
            final_tool="submit_doc",
        )

    assert submitted == [("submit_doc", {"markdown": "# The Doc"})]
    # The nudge carries the prose back, so the model has its own draft to hand over.
    assert sent[1]["messages"][-1]["role"] == "user"
    assert "submit_doc" in sent[1]["messages"][-1]["content"]


def test_a_forced_turn_offers_only_the_terminal_tool(monkeypatch):
    """Left the full list, a model told to finish reaches for one more read instead — a real
    endpoint did exactly that when asked to finish with every tool still on the table."""
    sent = _anthropic_turns(
        monkeypatch,
        [
            _reply("end_turn", _text_block("prose")),
            _reply("end_turn", _text_block("still prose")),
        ],
    )

    AnthropicProvider().converse(
        "go",
        tools=[_Tool("read_file"), _Tool("submit_doc")],
        invoke=lambda n, a: "",
        max_turns=8,
        final_tool="submit_doc",
    )

    assert [t["name"] for t in sent[0]["tools"]] == ["read_file", "submit_doc"]
    assert [t["name"] for t in sent[1]["tools"]] == ["submit_doc"]
    assert sent[1]["tool_choice"] == {"type": "tool", "name": "submit_doc"}


def test_the_nudge_is_sent_at_most_once(monkeypatch):
    """Otherwise a model that will not use the tool becomes a loop that bills until max_turns."""
    sent = _anthropic_turns(monkeypatch, [_reply("end_turn", _text_block("prose"))] * 8)

    said = AnthropicProvider().converse(
        "go",
        tools=[_Tool("submit_doc")],
        invoke=lambda n, a: "",
        max_turns=8,
        final_tool="submit_doc",
    )

    assert said == "prose"
    assert len(sent) == 2, "one ordinary turn, one nudge, then give up"


def test_no_terminal_tool_means_no_nudge(monkeypatch):
    """`converse` is general: a caller with nothing to force still gets one answer and stops."""
    sent = _anthropic_turns(monkeypatch, [_reply("end_turn", _text_block("just an answer"))])

    said = AnthropicProvider().converse("go", tools=[_Tool("x")], invoke=lambda n, a: "")

    assert said == "just an answer"
    assert len(sent) == 1


def test_openai_nudges_a_prose_answer_too(monkeypatch):
    call = {"id": "c1", "function": {"name": "submit_doc", "arguments": "{}"}}
    sent = _openai_turns(
        monkeypatch,
        [
            {"choices": [{"message": {"role": "assistant", "content": "# The Doc"}}]},
            {"choices": [{"message": {"role": "assistant", "tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ],
    )
    seen = []

    _openai_provider().converse(
        "go",
        tools=[_Tool("read_file"), _Tool("submit_doc")],
        invoke=lambda n, a: seen.append(n) or "",
        max_turns=8,
        final_tool="submit_doc",
    )

    assert seen == ["submit_doc"]
    assert sent[1]["messages"][-1]["role"] == "user"
    assert [t["function"]["name"] for t in sent[1]["tools"]] == ["submit_doc"]
    assert sent[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_doc"}}


def test_every_task_specky_names_is_a_task_something_asks_for():
    """A name in `TASKS` is a `[ai] <task>_model` key the config will accept, so a stale one routes
    nothing while `specky doctor` reports the route as active. That is the exact failure the tuple
    exists to prevent, and `discovery`/`glossary` sat here for a while after `specky bootstrap`
    — their only caller — was deleted."""
    import pathlib
    import re

    src = pathlib.Path(__file__).parent.parent / "src" / "specky"
    asked = {
        match
        for path in src.glob("*.py")
        for match in re.findall(r'task="([a-z]+)"', path.read_text())
    }

    assert set(ai_provider.TASKS) == asked, (
        f"in TASKS but never asked for: {sorted(set(ai_provider.TASKS) - asked)}; "
        f"asked for but not in TASKS: {sorted(asked - set(ai_provider.TASKS))}"
    )


def test_a_task_specky_no_longer_has_is_a_config_error(tmp_path):
    """Someone's `specky.toml` may still carry `discovery_model` from before bootstrap was removed.
    Failing loudly is the point — routing nothing in silence is what this check is for."""
    path = tmp_path / "specky.toml"
    path.write_text('[ai]\nprovider = "anthropic"\nmodel = "haiku"\ndiscovery_model = "sonnet"\n')

    with pytest.raises(ConfigError, match="names no task"):
        load_provider_from_toml(path)
