"""Nível de esforço (reasoning_effort) chegando ao chat template.

Puro: só thinking_policy, sem main.py (que exige fastapi e as env vars de
runtime, e por isso não roda local — ver test_thinking_policy.py)."""

import pytest

from thinking_policy import ThinkingPolicy, apply_thinking_policy, resolve_thinking_policy

MACHINE = {"model_name": "RedHatAI/Qwen3.8-27B-INT4", "served_model_name": "pro-base"}


def _apply(body):
    policy = resolve_thinking_policy(body, {}, None, MACHINE)
    apply_thinking_policy(body, policy)
    return policy, body


@pytest.mark.parametrize("pedido,template", [("low", "low"), ("medium", "medium"), ("high", "xhigh")])
def test_effort_top_level_vira_kwarg_do_template(pedido, template):
    policy, body = _apply({"reasoning_effort": pedido, "max_tokens": 12000})
    assert policy == ThinkingPolicy(True, "request", pedido)
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": template}


def test_effort_no_objeto_reasoning_tambem_vale():
    _, body = _apply({"reasoning": {"effort": "low"}})
    assert body["chat_template_kwargs"]["reasoning_effort"] == "low"


def test_effort_none_desliga_e_nao_manda_nivel():
    policy, body = _apply({"reasoning_effort": "none"})
    assert policy.effort is None
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_kwarg_explicito_do_cliente_vence_o_mapeamento():
    _, body = _apply({"reasoning_effort": "high",
                      "chat_template_kwargs": {"reasoning_effort": "medium"}})
    assert body["chat_template_kwargs"]["reasoning_effort"] == "medium"


def test_sem_effort_o_template_fica_no_default():
    _, body = _apply({"chat_template_kwargs": {"enable_thinking": True}})
    assert "reasoning_effort" not in body["chat_template_kwargs"]
