"""Testes do orçamento de contexto (context_budget.py) — funções puras, sem
precisar das env vars do main.py nem de rede. Rodar de docker/gateway/:

    python3 -m pytest test_context_budget.py
"""

import pytest

from context_budget import (
    CONTEXT_SAFETY_FACTOR,
    CONTEXT_TEMPLATE_OVERHEAD,
    ContextWindowExceeded,
    anthropic_error_body,
    apply_context_budget,
    estimate_prompt_tokens,
    openai_error_body,
    reserved_tokens_for,
    should_use_exact_token_count,
)

VIBECODER_WINDOW = 16384


def _machine(max_model_len=VIBECODER_WINDOW):
    return {"id": "m1", "max_model_len": max_model_len}


def test_clampa_abaixo_do_piso_quando_a_janela_exige():
    # cenário real do bug: Claude Code pede 16000 de saída numa janela de
    # 16384 — o clamp precisa reduzir pra menos que o piso de 8000, senão o
    # piso re-infla e o vLLM devolve o 400 cru
    prompt = "x" * 20_000  # ~5k tokens estimados
    body = {"max_tokens": 16_000, "messages": [{"role": "user", "content": prompt}]}
    est = estimate_prompt_tokens(messages=body["messages"])
    apply_context_budget(body, _machine(), est_tokens=est)
    expected_budget = VIBECODER_WINDOW - reserved_tokens_for(est)
    assert body["max_tokens"] <= expected_budget
    assert body["max_tokens"] < 16_000


def test_prompt_maior_que_a_janela_rejeita_com_erro_claro():
    prompt = "x" * 120_000  # ~30k tokens > janela de 16384
    body = {"max_tokens": 8_000, "messages": [{"role": "user", "content": prompt}]}
    est = estimate_prompt_tokens(messages=body["messages"])
    with pytest.raises(ContextWindowExceeded) as exc:
        apply_context_budget(body, _machine(), est_tokens=est)
    assert exc.value.status_code == 400
    assert "janela de contexto" in exc.value.detail
    assert "16384" in exc.value.detail


def test_sem_max_model_len_nao_toca_no_body():
    # pod anterior à migration 0031 ou template sem --max-model-len: sem
    # clamp, comportamento antigo preservado
    body = {"max_tokens": 16_000, "messages": [{"role": "user", "content": "x" * 200_000}]}
    for machine in ({"id": "m1"}, _machine(None), _machine(0)):
        apply_context_budget(body, machine, est_tokens=999_999)
        assert body["max_tokens"] == 16_000


def test_max_tokens_dentro_do_orcamento_fica_intocado():
    body = {"max_tokens": 1_000}
    apply_context_budget(body, _machine(), est_tokens=100)
    assert body["max_tokens"] == 1_000


def test_campo_ausente_nao_quebra_nem_cria():
    # embeddings e afins não têm max_tokens — clamp vira no-op
    body = {"input": "abc"}
    apply_context_budget(body, _machine(), est_tokens=100)
    assert "max_tokens" not in body


def test_campo_da_responses_api():
    body = {"max_output_tokens": 16_000}
    apply_context_budget(body, _machine(), field="max_output_tokens", est_tokens=3_000)
    assert body["max_output_tokens"] < 16_000


def test_tools_aumentam_a_estimativa():
    messages = [{"role": "user", "content": "oi"}]
    tools = [{"type": "function", "function": {"name": "grep", "description": "x" * 8_000}}]
    assert estimate_prompt_tokens(messages=messages, tools=tools) > estimate_prompt_tokens(
        messages=messages
    )


def test_imagens_base64_ficam_fora_da_estimativa():
    fake_b64 = "A" * 100_000
    with_image = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "descreva"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{fake_b64}"}},
            ],
        }
    ]
    without_image = [
        {"role": "user", "content": [{"type": "text", "text": "descreva"}]}
    ]
    est_with = estimate_prompt_tokens(messages=with_image)
    est_without = estimate_prompt_tokens(messages=without_image)
    assert est_with - est_without < 100  # a imagem não pode pesar ~25k tokens


def test_reserved_tokens_for_aplica_fator_e_overhead():
    est = 1_000
    assert reserved_tokens_for(est) == int(est * CONTEXT_SAFETY_FACTOR + 0.999999) + CONTEXT_TEMPLATE_OVERHEAD


PRO_WINDOW = 65_536


def test_should_use_exact_token_count_longe_do_limite_fica_false():
    # prompt pequeno numa janela de 65536 — nem perto do threshold (0.7 default)
    assert should_use_exact_token_count(1_000, _machine(PRO_WINDOW)) is False


def test_should_use_exact_token_count_reproduz_incidente():
    # cenário real: heurística ~54730 contra janela de 65536 — é exatamente o
    # caso em que a heurística já rejeitaria e vale a pena confirmar com a
    # contagem real do tokenizer antes de decidir
    assert should_use_exact_token_count(54_730, _machine(PRO_WINDOW)) is True


def test_should_use_exact_token_count_sem_max_model_len_fica_false():
    for machine in ({"id": "m1"}, _machine(None), _machine(0)):
        assert should_use_exact_token_count(999_999, machine) is False


def test_shapes_de_erro():
    a = anthropic_error_body("msg")
    assert a["type"] == "error" and a["error"]["type"] == "invalid_request_error"
    o = openai_error_body("msg")
    assert o["error"]["code"] == "context_length_exceeded"
