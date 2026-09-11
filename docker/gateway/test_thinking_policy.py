import asyncio
import copy
import os

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import main
from anthropic_compat import anthropic_to_openai_request
from reasoning_filter import clean_completion_payload
from thinking_policy import ThinkingPolicyError, resolve_thinking_policy

MACHINE = {"model_name": "Qwen/Qwen3.5-9B", "served_model_name": "go-base", "max_model_len": 32768}


def entry(**stack_fields):
    return {"account_id": "a1", "api_key_id": "k1", "stack_id": "s1",
            "stacks": [{"id": "s1", "plan": "Go", **stack_fields}]}


def validate(body, config=None):
    return asyncio.run(main.validate_body(copy.deepcopy(body), config or entry(), False, MACHINE, "s1"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("limit", [100, 2048, 12000])
def test_explicit_policy_is_independent_of_output_length(enabled, limit):
    result = validate({"max_tokens": limit}, entry(default_enable_thinking=enabled))
    assert result["max_tokens"] == limit
    assert main.thinking_esperado_de(result) is enabled


def test_request_then_key_then_stack_precedence():
    config = entry(default_enable_thinking=True)
    config["default_enable_thinking"] = False
    assert main.thinking_esperado_de(validate({"max_tokens": 100}, config)) is False
    assert main.thinking_esperado_de(validate({"max_tokens": 100,
        "chat_template_kwargs": {"enable_thinking": True}}, config)) is True


def test_legacy_and_migration_keep_node_direct():
    for config in (entry(), entry(default_enable_thinking=False)):
        assert main.thinking_esperado_de(validate({"max_tokens": 2048}, config)) is False
    assert main.thinking_esperado_de(validate({})) is True
    assert validate({})["max_tokens"] == main.MIN_MAX_TOKENS


@pytest.mark.parametrize("field,value", [("reasoning_effort", "none"), ("reasoning", {"effort": "none"})])
def test_protocol_effort_sets_actual_switch(field, value):
    assert main.thinking_esperado_de(validate({field: value, "max_tokens": 12000})) is False


@pytest.mark.parametrize("body", [
    {"chat_template_kwargs": {"enable_thinking": "false"}},
    {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "high"},
    {"reasoning_effort": "none", "reasoning": {"effort": "high"}},
    {"thinking": {"type": "enabled", "budget_tokens": 1000}},
])
def test_bad_or_conflicting_policy_is_rejected(body):
    with pytest.raises(main.HTTPException) as exc:
        validate(body)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("name", ["Qwen/Qwen3-Coder-30B", "Qwen/Qwen3-4B-Thinking-2507", "unknown"])
def test_unsupported_checkpoints_reject_explicit_switch(name):
    with pytest.raises(ThinkingPolicyError):
        resolve_thinking_policy({"chat_template_kwargs": {"enable_thinking": False}}, {}, {}, {"model_name": name})
    assert resolve_thinking_policy({}, {}, {}, {"model_name": name}).enabled is None


# Os checkpoints que realmente rodam hoje. Sem isto a política passava só em
# 3.5 e o Pro (3.8) rejeitava toda request de uma stack configurada.
@pytest.mark.parametrize("name", [
    "Qwen/Qwen3.5-9B",
    "RedHatAI/Qwen3.5-9B-quantized.w4a16",
    "Qwen/Qwen3.6-27B-FP8",
    "Qwen/Qwen3.8-27B-FP8",
])
@pytest.mark.parametrize("enabled", [False, True])
def test_production_checkpoints_accept_explicit_switch(name, enabled):
    policy = resolve_thinking_policy(
        {"chat_template_kwargs": {"enable_thinking": enabled}}, {}, {}, {"model_name": name})
    assert policy.enabled is enabled
    assert policy.source == "request"


@pytest.mark.parametrize("enabled", [False, True])
def test_anthropic_conversion_preserves_explicit_activation(enabled):
    body, _ = anthropic_to_openai_request({"model": "go-base", "max_tokens": 100,
        "thinking": {"type": "enabled" if enabled else "disabled"}})
    result = validate(body)
    assert main.thinking_esperado_de(result) is enabled
    assert "thinking" not in result


@pytest.mark.parametrize("enabled", [False, True])
def test_responses_honors_small_limit_with_explicit_policy(monkeypatch, enabled):
    async def estimate(*args, **kwargs):
        return 100, None
    monkeypatch.setattr(main, "resolve_est_tokens", estimate)
    monkeypatch.setattr(main, "apply_context_budget", lambda *a, **kw: None)
    config = entry(default_enable_thinking=enabled)
    result = asyncio.run(main.validate_responses_body(
        {"instructions": "client system", "max_output_tokens": 100}, config, False, MACHINE, "s1"))
    assert result["max_output_tokens"] == 100
    assert main.thinking_esperado_de(result) is enabled


def test_max_completion_tokens_alias_reaches_limit_policy():
    assert validate({"max_completion_tokens": 2048})["max_tokens"] == 2048


@pytest.mark.parametrize("parser", [None, "reasoning", "reasoning_content"])
def test_filter_uses_effective_off_policy_without_hiding_answer(parser):
    request = validate({"max_tokens": 2048}, entry(default_enable_thinking=False))
    message = {"content": "Opa!"}
    if parser:
        message[parser] = ""
    payload, empty = clean_completion_payload({"choices": [{"message": message}],
        "usage": {"completion_tokens": 4}}, status_code=200,
        thinking_esperado=main.thinking_esperado_de(request))
    assert not empty
    assert payload["choices"][0]["message"]["content"] == "Opa!"
    assert payload["usage"]["completion_tokens"] == 4
