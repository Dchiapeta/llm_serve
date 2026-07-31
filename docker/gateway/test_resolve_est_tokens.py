"""Testes de resolve_est_tokens — a escolha entre heurística e contagem real do
tokenizer, e a procedência (EstimateKind) que decide a margem de segurança.

    python3 -m pytest test_resolve_est_tokens.py

Mora em context_budget (não em main.py) justamente pra ter estes testes: o main
exige SUPABASE_URL & cia. no import, então nada dele é testável sem infra.
"""

import asyncio

from context_budget import (
    CONTEXT_IMAGE_TOKENS,
    EstimateKind,
    resolve_est_tokens,
)

PRO_WINDOW = 65_536


def _machine(**over):
    return {"id": "m1", "max_model_len": PRO_WINDOW, "served_model_name": "pro-base", **over}


def _run(machine, heuristic_est, tokenize_fn, image_tokens=0):
    return asyncio.run(
        resolve_est_tokens(
            machine, heuristic_est, "texto", tokenize_fn, image_tokens=image_tokens
        )
    )


def _returning(value):
    async def fn(machine, model, text):
        return value

    return fn


def _exploding():
    async def fn(machine, model, text):
        raise AssertionError("não deveria chamar o tokenizer longe do limite")

    return fn


def test_longe_do_limite_usa_a_heuristica_sem_chamada_de_rede():
    """O fast-path é o ponto: a maioria das requests sobra de sobra e não pode
    pagar um round-trip gateway -> agent -> vLLM só pra confirmar isso."""
    est, kind = _run(_machine(), 1_000, _exploding())
    assert (est, kind) == (1_000, EstimateKind.HEURISTIC)


def test_perto_do_limite_usa_a_contagem_do_tokenizer():
    est, kind = _run(_machine(), 54_730, _returning(48_000))
    assert (est, kind) == (48_000, EstimateKind.EXACT)


def test_contagem_exata_soma_os_tokens_de_imagem():
    """exact_text sai de prompt_text_for_tokenize, que DESCARTA as imagens
    (mandar base64 pro tokenizer seria uma chamada enorme e uma contagem
    errada). Sem somar o custo delas por fora, perto do limite um prompt com
    imagem passava a valer MENOS do que valia pela heurística — o oposto do que
    CONTEXT_IMAGE_TOKENS existe pra fazer."""
    image_tokens = 2 * CONTEXT_IMAGE_TOKENS
    est, kind = _run(_machine(), 54_730, _returning(48_000), image_tokens=image_tokens)
    assert (est, kind) == (48_000 + image_tokens, EstimateKind.EXACT)


def test_tokenizer_indisponivel_cai_na_heuristica_como_fallback():
    """Pod fora do ar, agent antigo sem /tokenize, timeout: a checagem extra
    nunca pode travar a request por conta própria. Mas o kind é FALLBACK, não
    HEURISTIC — a margem tem que ser mais conservadora que a da contagem exata,
    já que o cliente foi instruído a mandar até um volume que só é admissível
    com contagem exata."""
    est, kind = _run(_machine(), 54_730, _returning(None))
    assert (est, kind) == (54_730, EstimateKind.FALLBACK)


def test_maquina_sem_nome_de_modelo_servido_e_fallback():
    machine = _machine(served_model_name=None)
    machine.pop("model_name", None)
    est, kind = _run(machine, 54_730, _exploding())
    assert (est, kind) == (54_730, EstimateKind.FALLBACK)


def test_cai_no_model_name_quando_nao_ha_served_model_name():
    """Template sem --served-model-name: o vLLM serve pelo próprio --model."""
    machine = _machine(served_model_name=None, model_name="Qwen/Qwen3.6-27B-FP8")
    est, kind = _run(machine, 54_730, _returning(50_000))
    assert (est, kind) == (50_000, EstimateKind.EXACT)


def test_sem_janela_conhecida_nunca_paga_a_contagem():
    """Pod anterior à migration 0031: sem max_model_len o clamp é no-op, então
    não há decisão nenhuma que a contagem exata pudesse mudar."""
    est, kind = _run(_machine(max_model_len=None), 999_999, _exploding())
    assert (est, kind) == (999_999, EstimateKind.HEURISTIC)
