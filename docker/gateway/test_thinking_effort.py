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
    # o campo do protocolo NÃO pode sobreviver: cru no corpo, ele chega ao jinja
    # e derruba a request (400 "Unexpected reasoning effort high"), visto em
    # produção em 13/09/2026
    assert "reasoning_effort" not in body


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


def test_top_level_sai_mesmo_quando_o_cliente_fixou_o_kwarg():
    """O kwarg explícito do cliente vence, mas o top-level ainda tem que sair —
    senão os dois chegam juntos e o cru é o que o template lê."""
    _, body = _apply({"reasoning_effort": "high",
                      "chat_template_kwargs": {"reasoning_effort": "medium"}})
    assert body["chat_template_kwargs"]["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in body


def test_effort_none_tambem_limpa_o_top_level():
    _, body = _apply({"reasoning_effort": "none"})
    assert "reasoning_effort" not in body


def test_objeto_reasoning_da_responses_api_nao_e_tocado():
    """`reasoning` é campo da Responses API, servida nativamente pelo vLLM —
    mexer nele quebraria o Codex."""
    _, body = _apply({"reasoning": {"effort": "low"}})
    assert body["reasoning"] == {"effort": "low"}


# ---------- default_reasoning_effort da chave (migration 0069) ----------

def _apply_com_chave(body, entry, stack=None):
    policy = resolve_thinking_policy(body, entry, stack, MACHINE)
    apply_thinking_policy(body, policy)
    return policy, body


@pytest.mark.parametrize("nivel,template", [("low", "low"), ("medium", "medium"), ("high", "xhigh")])
def test_nivel_da_chave_liga_e_manda_o_nivel(nivel, template):
    policy, body = _apply_com_chave({"max_tokens": 12000}, {"default_reasoning_effort": nivel})
    assert policy == ThinkingPolicy(True, "key", nivel)
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": template}


def test_none_na_chave_desliga():
    policy, body = _apply_com_chave({}, {"default_reasoning_effort": "none"})
    assert policy == ThinkingPolicy(False, "key")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_nivel_da_chave_ganha_do_boolean_da_chave_e_da_stack():
    entry = {"default_reasoning_effort": "low", "default_enable_thinking": False}
    policy, _ = _apply_com_chave({}, entry, {"default_enable_thinking": False})
    assert policy == ThinkingPolicy(True, "key", "low")


def test_null_na_chave_cai_no_boolean():
    entry = {"default_reasoning_effort": None, "default_enable_thinking": True}
    policy, body = _apply_com_chave({}, entry)
    assert policy == ThinkingPolicy(True, "key")
    assert "reasoning_effort" not in body["chat_template_kwargs"]


def test_request_explicita_ganha_do_nivel_da_chave():
    policy, body = _apply_com_chave({"reasoning_effort": "low"}, {"default_reasoning_effort": "high"})
    assert policy == ThinkingPolicy(True, "request", "low")
    assert body["chat_template_kwargs"]["reasoning_effort"] == "low"
    policy, body = _apply_com_chave({"reasoning_effort": "none"}, {"default_reasoning_effort": "high"})
    assert policy == ThinkingPolicy(False, "request")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_request_que_so_liga_recebe_o_nivel_da_chave():
    """`enable_thinking: true` (ou `thinking.type: enabled`) sem nível: o
    nível da chave preenche, como um default de sampling preencheria."""
    policy, body = _apply_com_chave({"chat_template_kwargs": {"enable_thinking": True}},
                                    {"default_reasoning_effort": "high"})
    assert policy == ThinkingPolicy(True, "request", "high")
    assert body["chat_template_kwargs"]["reasoning_effort"] == "xhigh"
    policy, _ = _apply_com_chave({"thinking": {"type": "enabled"}}, {"default_reasoning_effort": "medium"})
    assert policy.effort == "medium"


def test_request_que_desliga_ignora_o_nivel_da_chave():
    policy, body = _apply_com_chave({"chat_template_kwargs": {"enable_thinking": False}},
                                    {"default_reasoning_effort": "high"})
    assert policy == ThinkingPolicy(False, "request")
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_nivel_invalido_na_chave_e_erro():
    from thinking_policy import ThinkingPolicyError
    with pytest.raises(ThinkingPolicyError):
        resolve_thinking_policy({}, {"default_reasoning_effort": "xhigh"}, None, MACHINE)
