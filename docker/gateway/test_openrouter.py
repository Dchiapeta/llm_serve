"""Testes de openrouter.py — a parte pura do repasse para o OpenRouter.

Os payloads seguem os formatos documentados pelo OpenRouter em 09/2026: usage
com `cost` no chunk final do chat, `response.usage` no response.completed e o
par message_start/message_delta do endpoint Anthropic.

Rodar: pytest docker/gateway/test_openrouter.py -q
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from openrouter import (  # noqa: E402
    ANTHROPIC_SSE_PING,
    CostStripper,
    DECISIONS_KIND,
    ModelNameRewriter,
    IMAGE_KIND,
    TEXT_KIND,
    UsageCostScanner,
    accepted_models,
    anthropic_system_text,
    data_url,
    decisions_url,
    error_of,
    image_body,
    pick_slug,
    pick_target,
    plan_catalog,
    plan_denial,
    prepare_anthropic_body,
    prepare_decisions_body,
    prepare_openai_body,
    requested_model,
    stream_error_frame,
    strip_cost_body,
    unavailable_detail,
    usage_and_cost,
    with_heartbeat,
)

CATALOG = {
    "anthropic/claude-sonnet-4.5": TEXT_KIND,
    "qwen/qwen3-coder": TEXT_KIND,
    "google/gemini-2.5-flash-image": IMAGE_KIND,
    "typesafe/jev-1.13": DECISIONS_KIND,
}


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


# ---------- catálogo ----------


def test_requested_model_bytes_e_dict():
    assert requested_model(b'{"model": " qwen/qwen3-coder "}') == "qwen/qwen3-coder"
    assert requested_model({"model": "x"}) == "x"
    assert requested_model(b"nao-json") is None
    assert requested_model({"model": ""}) is None
    assert requested_model(None) is None


def test_pick_slug_respeita_tipo():
    assert pick_slug("qwen/qwen3-coder", TEXT_KIND, CATALOG) == "qwen/qwen3-coder"
    # modelo de imagem não atende chat, e vice-versa
    assert pick_slug("google/gemini-2.5-flash-image", TEXT_KIND, CATALOG) is None
    assert pick_slug("qwen/qwen3-coder", IMAGE_KIND, CATALOG) is None
    # o Jev só atende o /v1/decisions, e o /v1/decisions só atende o Jev
    assert pick_slug("typesafe/jev-1.13", TEXT_KIND, CATALOG) is None
    assert pick_slug("typesafe/jev-1.13", DECISIONS_KIND, CATALOG) == "typesafe/jev-1.13"
    assert pick_slug("qwen/qwen3-coder", DECISIONS_KIND, CATALOG) is None
    assert pick_slug("fora/da-lista", TEXT_KIND, CATALOG) is None
    assert pick_slug(None, TEXT_KIND, CATALOG) is None


TODOS = ["Go", "Pro", "Max", "Enterprise"]
ROWS = [
    {"slug": "z-ai/glm-5.3-flash", "kind": TEXT_KIND, "plans": TODOS, "fallback_plans": []},
    {"slug": "qwen/qwen3.8-27b", "kind": TEXT_KIND, "plans": ["Pro", "Max", "Enterprise"],
     "fallback_plans": ["Pro", "Max", "Enterprise"], "fallback": True},
    {"slug": "qwen/qwen3.5-9b", "kind": TEXT_KIND, "plans": TODOS, "fallback_plans": ["Go"]},
    {"slug": "qwen/qwen-image-3", "kind": IMAGE_KIND, "plans": TODOS, "fallback_plans": []},
    # ainda não lançado: na lista, sem plano nenhum
    {"slug": "moonshotai/kimi-k3", "kind": TEXT_KIND, "plans": [], "fallback_plans": []},
]


def test_plan_catalog_recorta_modelos_e_reserva_por_plano():
    catalog, fallbacks = plan_catalog(ROWS, "Go")
    assert accepted_models(catalog, TEXT_KIND) == ["qwen/qwen3.5-9b", "z-ai/glm-5.3-flash"]
    # o reserva do Go é o modelo da máquina do Go, não o 27B (que não é do Go)
    assert fallbacks == {TEXT_KIND: "qwen/qwen3.5-9b"}
    catalog, fallbacks = plan_catalog(ROWS, "Pro")
    assert "qwen/qwen3.8-27b" in catalog
    assert fallbacks == {TEXT_KIND: "qwen/qwen3.8-27b"}
    # chave sem plano não alcança modelo nenhum que declare planos
    assert plan_catalog(ROWS, None) == ({}, {})


def test_plan_catalog_linha_anterior_a_0074_vale_para_todos():
    legado = [{"slug": "qwen/qwen3.8-27b", "kind": TEXT_KIND, "fallback": True}]
    assert plan_catalog(legado, "Go") == (
        {"qwen/qwen3.8-27b": TEXT_KIND}, {TEXT_KIND: "qwen/qwen3.8-27b"}
    )


def test_plan_denial_so_nega_modelo_da_lista_fora_do_plano():
    assert plan_denial("qwen/qwen3.8-27b", TEXT_KIND, ROWS, "Pro") is None
    assert plan_denial("qwen/qwen3.5-9b", TEXT_KIND, ROWS, "Go") is None
    # fora da lista não é negado: segue a regra de destino (máquina responde)
    assert plan_denial("go-base", TEXT_KIND, ROWS, "Go") is None
    assert plan_denial(None, TEXT_KIND, ROWS, "Go") is None
    # modelo de outro tipo não é o pedido desta rota
    assert plan_denial("qwen/qwen-image-3", TEXT_KIND, ROWS, None) is None
    # linha anterior à 0074 (sem plans) vale para todos
    assert plan_denial("x/y", TEXT_KIND, [{"slug": "x/y", "kind": TEXT_KIND}], "Go") is None


def test_plan_denial_lista_o_que_o_plano_tem():
    msg = plan_denial("qwen/qwen3.8-27b", TEXT_KIND, ROWS, "Go")
    assert "'qwen/qwen3.8-27b'" in msg and "plano Go" in msg
    assert "qwen/qwen3.5-9b, z-ai/glm-5.3-flash" in msg


def test_modelo_sem_plano_nenhum_nao_esta_disponivel_para_ninguem():
    for plan in TODOS + [None]:
        msg = plan_denial("moonshotai/kimi-k3", TEXT_KIND, ROWS, plan)
        assert msg == "o modelo 'moonshotai/kimi-k3' ainda não está disponível"
    # e não aparece no catálogo de plano nenhum (nem em /v1/models)
    assert all("moonshotai/kimi-k3" not in plan_catalog(ROWS, p)[0] for p in TODOS)


def test_accepted_models_e_mensagem():
    aceitos = accepted_models(CATALOG, TEXT_KIND)
    assert aceitos == ["anthropic/claude-sonnet-4.5", "qwen/qwen3-coder"]
    msg = unavailable_detail("gpt-x", aceitos)
    assert "'gpt-x'" in msg and "qwen/qwen3-coder" in msg
    assert "não está disponível no momento" in unavailable_detail("gpt-x", [])


# ---------- corpo ----------


def test_prepare_openai_body_trava_modelo_e_teto():
    body = prepare_openai_body(
        {"model": "outro", "max_tokens": 100000, "n": 5,
         "chat_template_kwargs": {"enable_thinking": False}, "cache_salt": "x"},
        "qwen/qwen3-coder", 32000,
    )
    assert body["model"] == "qwen/qwen3-coder"
    assert body["max_tokens"] == 32000
    assert body["n"] == 1
    assert "chat_template_kwargs" not in body and "cache_salt" not in body


def test_prepare_openai_body_nao_mexe_abaixo_do_teto_nem_em_bool():
    body = prepare_openai_body({"max_output_tokens": 500, "max_tokens": True}, "m", 32000)
    assert body["max_output_tokens"] == 500
    assert body["max_tokens"] is True


def test_prepare_anthropic_body_ajusta_orcamento_de_thinking():
    body = prepare_anthropic_body(
        {"max_tokens": 64000, "thinking": {"type": "enabled", "budget_tokens": 40000}},
        "anthropic/claude-sonnet-4.5", 32000,
    )
    assert body["max_tokens"] == 32000
    assert body["thinking"]["budget_tokens"] == 31999


def test_prepare_anthropic_body_remove_thinking_sem_espaco():
    body = prepare_anthropic_body(
        {"max_tokens": 900, "thinking": {"type": "enabled", "budget_tokens": 2000}},
        "m", 32000,
    )
    assert "thinking" not in body


def test_anthropic_system_text():
    assert anthropic_system_text("oi") == "oi"
    assert anthropic_system_text([
        {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
        {"type": "image"},
        {"type": "text", "text": "b"},
    ]) == "a\n\nb"
    assert anthropic_system_text(None) == ""


def test_image_body_generations():
    body = image_body(
        {"prompt": "ignorado", "n": 9, "size": "1024x1024", "steps": 8,
         "response_format": "url", "seed": 42},
        slug="google/gemini-2.5-flash-image", prompt="um gato",
    )
    assert body == {
        "model": "google/gemini-2.5-flash-image", "prompt": "um gato",
        "n": 4, "size": "1024x1024", "seed": 42,
    }


def test_image_body_edits_converte_form_e_referencias():
    ref = data_url("image/jpeg", b"\xff\xd8abc")
    body = image_body(
        {"n": "2", "seed": "abc", "quality": "high"},
        slug="m", prompt="troca o fundo", references=[ref],
    )
    assert body["n"] == 2
    assert "seed" not in body  # inteiro ilegível é descartado, não repassado
    assert body["input_references"] == [{"type": "image_url", "image_url": {"url": ref}}]
    assert ref.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(ref.split(",", 1)[1]) == b"\xff\xd8abc"


def test_decisions_url_sai_do_v1():
    assert decisions_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/alpha/decisions"
    # httpx normaliza o base_url com barra no fim
    assert decisions_url("https://openrouter.test/api/v1/") == "https://openrouter.test/api/alpha/decisions"


def test_prepare_decisions_body_trava_modelo_e_tira_stream():
    body = prepare_decisions_body(
        {"model": "qualquer", "stream": True, "state": "s", "questions": {"q": {}},
         "session_id": "abc"},
        "typesafe/jev-1.13",
    )
    assert body == {"model": "typesafe/jev-1.13", "state": "s", "questions": {"q": {}},
                    "session_id": "abc"}


def test_decisions_usage_vira_formato_chat_com_custo():
    raw = json.dumps({"answers": {}, "usage": {
        "input_tokens": 476, "output_tokens": 70, "cost": 0.000019992}}).encode()
    usage, cost = usage_and_cost(raw)
    assert usage["prompt_tokens"] == 476 and usage["completion_tokens"] == 70
    assert cost == 0.000019992
    assert b"cost" not in strip_cost_body(raw)


# ---------- resposta ----------


def test_error_of():
    assert error_of({"error": {"code": 502, "message": "Provider down"}}) == "Provider down"
    assert error_of({"choices": []}) is None
    assert error_of("x") is None


def test_scanner_chat_stream_com_custo():
    s = UsageCostScanner()
    s.feed(b": OPENROUTER PROCESSING\n\n")
    s.feed(_sse({"choices": [{"delta": {"content": "oi"}}]}))
    final = _sse({
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                  "cost": 0.00014},
    })
    # linha cortada no meio entre dois chunks
    s.feed(final[:30])
    s.feed(final[30:])
    s.feed(b"data: [DONE]\n\n")
    usage, cost = s.finish()
    assert usage["prompt_tokens"] == 10 and usage["completion_tokens"] == 4
    assert cost == 0.00014


def test_scanner_responses():
    s = UsageCostScanner()
    s.feed(_sse({"type": "response.created", "response": {"usage": None}}))
    s.feed(_sse({"type": "response.completed", "response": {
        "usage": {"input_tokens": 12, "output_tokens": 45, "total_tokens": 57, "cost": 0.002}}}))
    usage, cost = s.finish()
    assert usage["prompt_tokens"] == 12 and usage["completion_tokens"] == 45
    assert cost == 0.002


def test_scanner_anthropic_mescla_start_e_delta_com_cache():
    s = UsageCostScanner()
    s.feed(b"event: message_start\n" + _sse({"type": "message_start", "message": {
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": None, "output_tokens": 1}}}))
    s.feed(b"event: message_delta\n" + _sse({"type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 300, "cost": 0.01}}))
    usage, cost = s.finish()
    assert usage["prompt_tokens"] == 1020
    assert usage["completion_tokens"] == 300
    assert cost == 0.01


def test_scanner_detecta_erro_no_meio_do_stream():
    s = UsageCostScanner()
    s.feed(_sse({"choices": [{"delta": {"content": 'o texto fala de "error"'}}]}))
    assert s.error is None  # a palavra no conteúdo não é um erro
    s.feed(_sse({"error": {"code": 429, "message": "Rate limit exceeded"},
                 "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]}))
    assert s.error == "Rate limit exceeded"


def test_usage_and_cost_nao_streamed_e_corpo_ruim():
    raw = json.dumps({"usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 1}}).encode()
    usage, cost = usage_and_cost(raw)
    assert usage["total_tokens"] == 5 and cost == 1.0
    assert usage_and_cost(b"<html>") == (None, None)


def test_stream_error_frame():
    anth = stream_error_frame("caiu", "upstream_disconnect", anthropic=True)
    assert anth.startswith(b"event: error\ndata: ")
    oa = stream_error_frame("caiu", "upstream_disconnect", anthropic=False)
    assert oa.endswith(b"data: [DONE]\n\n")


def test_strip_cost_body():
    raw = json.dumps({"choices": [], "usage": {
        "prompt_tokens": 1, "completion_tokens": 2, "cost": 0.5,
        "cost_details": {"x": 1}, "is_byok": False}}).encode()
    out = json.loads(strip_cost_body(raw))
    assert out["usage"] == {"prompt_tokens": 1, "completion_tokens": 2}
    intacto = b'{"choices": []}'
    assert strip_cost_body(intacto) is intacto


def test_cost_stripper_sse_com_linha_partida():
    final = _sse({"choices": [], "usage": {"prompt_tokens": 1, "cost": 0.1}})
    delta = _sse({"choices": [{"delta": {"content": 'custo "cost" no texto'}}]})
    st = CostStripper()
    out = st.feed(delta + final[:20]) + st.feed(final[20:]) + st.feed(b"data: [DONE]\n\n")
    out += st.flush()
    assert b'"cost":' not in out and b'"cost": ' not in out
    assert delta in out  # o delta sem usage passa byte a byte
    assert out.endswith(b"data: [DONE]\n\n")
    linhas = [l for l in out.split(b"\n") if l.startswith(b"data: {") and b"usage" in l]
    assert json.loads(linhas[0][6:])["usage"] == {"prompt_tokens": 1}


def test_cost_stripper_anthropic_message_delta():
    ev = b"event: message_delta\n" + _sse({"type": "message_delta", "usage": {
        "output_tokens": 5, "cost": 0.2}})
    st = CostStripper()
    out = st.feed(ev) + st.flush()
    assert out.startswith(b"event: message_delta\n")
    assert b"cost" not in out and b'"output_tokens": 5' in out


# ---------- heartbeat ----------


async def _collect(gen):
    return [chunk async for chunk in gen]


def test_heartbeat_injeta_ping_so_na_fronteira():
    async def fonte():
        yield b"event: message_start\ndata: {}\n\n"
        await asyncio.sleep(0.05)       # silêncio na fronteira → ping
        yield b"event: content_block_delta\ndata: {"  # evento pela metade
        await asyncio.sleep(0.05)       # silêncio NO MEIO → sem ping
        yield b"}\n\n"

    out = asyncio.run(_collect(with_heartbeat(fonte(), ping=ANTHROPIC_SSE_PING, interval_s=0.02)))
    joined = b"".join(out)
    assert ANTHROPIC_SSE_PING in out
    # nenhum ping partiu o evento do meio
    assert b"data: {}\n\n" in joined
    assert b"event: content_block_delta\ndata: {}\n\n" in joined
    idx_meio = out.index(b"event: content_block_delta\ndata: {")
    assert out[idx_meio + 1] == b"}\n\n"


def test_heartbeat_propaga_erro_da_fonte():
    class Boom(Exception):
        pass

    async def fonte():
        yield b"data: {}\n\n"
        raise Boom()

    async def run():
        try:
            await _collect(with_heartbeat(fonte(), ping=b": ping\n\n", interval_s=1))
        except Boom:
            return True
        return False

    assert asyncio.run(run())


# ---------- reserva e troca de nome (30/09) ----------


def test_pick_target_pedido_ganha_do_reserva():
    fallbacks = {TEXT_KIND: "qwen/qwen3.8-27b"}
    assert pick_target("qwen/qwen3-coder", TEXT_KIND, CATALOG, fallbacks) == "qwen/qwen3-coder"
    assert pick_target("pro-base", TEXT_KIND, CATALOG, fallbacks) == "qwen/qwen3.8-27b"
    assert pick_target(None, TEXT_KIND, CATALOG, fallbacks) == "qwen/qwen3.8-27b"
    # sem reserva de imagem configurado, modelo fora da lista não tem destino
    assert pick_target("go-image", IMAGE_KIND, CATALOG, fallbacks) is None


def test_model_name_rewriter_sse_e_corpo_inteiro():
    rw = ModelNameRewriter("pro-base", "moonshotai/kimi-k3")
    chunk1 = b'data: {"id":"x","model":"pro-base","choices":[{"delta":{"content":"o \\"model\\":\\"pro-base\\""}}]}\n\n'
    out = rw.feed(chunk1[:25]) + rw.feed(chunk1[25:]) + rw.flush()
    assert b'"model":"moonshotai/kimi-k3"' in out
    # o texto gerado (aspas escapadas) não é tocado
    assert b'\\"model\\":\\"pro-base\\"' in out
    assert rw.whole(b'{"model": "pro-base", "x": 1}') == b'{"model": "moonshotai/kimi-k3", "x": 1}'
