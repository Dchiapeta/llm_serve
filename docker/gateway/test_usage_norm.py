"""Testes de usage_norm.py — o módulo que faz os dois protocolos generativos
do vLLM (chat/completions e responses) desembocarem na mesma contabilidade.

Os payloads de Responses aqui são a forma real emitida por
vllm/entrypoints/openai/responses/serving.py (ResponseUsage dentro do snapshot
`response` do evento "response.completed"), não uma aproximação.

Rodar: pytest docker/gateway/test_usage_norm.py -q  (o arquivo é o mesmo em
docker/agent/usage_norm.py — cópia sincronizada, ver docstring do módulo).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from usage_norm import (  # noqa: E402
    SseUsageScanner,
    normalize_usage,
    usage_from_event,
    usage_from_sse_line,
)


def _responses_usage(input_tokens=1200, output_tokens=340, cached=1024):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "input_tokens_details": {
            "cached_tokens": cached,
            "input_tokens_per_turn": [input_tokens],
            "cached_tokens_per_turn": [cached],
        },
        "output_tokens_details": {
            "reasoning_tokens": 120,
            "tool_output_tokens": 0,
        },
    }


_DEFAULT = object()  # sentinela: usage=None é um caso de teste, não "use o default"


def _completed_event(usage=_DEFAULT, text="ok", event_type="response.completed"):
    return {
        "type": event_type,
        "sequence_number": 42,
        "response": {
            "id": "resp_abc",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": _responses_usage() if usage is _DEFAULT else usage,
        },
    }


# ---------- normalize_usage ----------


def test_formato_chat_passa_intacto():
    chat = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    assert normalize_usage(chat) == chat


def test_normalizacao_e_idempotente():
    # log_gateway_request chama isto no ponto de gravação, sobre usage que pode
    # já ter passado pela normalização no ponto de captura
    once = normalize_usage(_responses_usage())
    assert normalize_usage(once) == once


def test_responses_traduz_nomes_das_contagens():
    out = normalize_usage(_responses_usage(input_tokens=1200, output_tokens=340))
    assert out["prompt_tokens"] == 1200
    assert out["completion_tokens"] == 340
    assert out["total_tokens"] == 1540


def test_cached_tokens_vem_de_input_tokens_details():
    # o agent lê prompt_tokens_details.cached_tokens (cached_tokens_of); no
    # Responses o mesmo dado mora em input_tokens_details
    out = normalize_usage(_responses_usage(cached=1024))
    assert out["prompt_tokens_details"] == {"cached_tokens": 1024}


def test_sem_details_nao_inventa_cached():
    # ausência != zero: sem --enable-prompt-tokens-details o dado não existe, e
    # um zero fabricado aqui viraria "0% de cache hit" no log do agent
    out = normalize_usage({"input_tokens": 5, "output_tokens": 1})
    assert "prompt_tokens_details" not in out


def test_total_ausente_e_derivado():
    out = normalize_usage({"prompt_tokens": 7, "completion_tokens": 2})
    assert out["total_tokens"] == 9


def test_bloco_sem_contagem_nenhuma_e_none():
    # os eventos response.created/in_progress carregam "usage": null, e um dict
    # de zeros vindo deles sobrescreveria o usage real do evento final
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage({"total_tokens": 10}) is None
    assert normalize_usage("nao é dict") is None


def test_contagem_parcial_conta_como_contagem():
    out = normalize_usage({"input_tokens": 900})
    assert out["prompt_tokens"] == 900
    assert out["completion_tokens"] == 0


# ---------- usage_from_event ----------


def test_evento_completed_do_responses():
    out = usage_from_event(_completed_event())
    assert (out["prompt_tokens"], out["completion_tokens"]) == (1200, 340)


def test_eventos_intermediarios_do_responses_nao_zeram():
    # response.created/in_progress têm a MESMA estrutura, com usage nulo
    for event_type in ("response.created", "response.in_progress"):
        evento = _completed_event(usage=None, event_type=event_type)
        assert usage_from_event(evento) is None
        assert usage_from_sse_line(_sse(evento)) is None


def test_chunk_final_do_chat_streaming():
    chunk = {
        "id": "chatcmpl-1",
        "choices": [],
        "usage": {"prompt_tokens": 999, "completion_tokens": 7, "total_tokens": 1006},
    }
    assert usage_from_event(chunk)["prompt_tokens"] == 999


def test_resposta_nao_streamed_do_responses():
    # mesmo objeto do evento, sem envelope: é o corpo do POST /v1/responses
    body = _completed_event()["response"]
    assert usage_from_event(body)["completion_tokens"] == 340


# ---------- usage_from_sse_line ----------


def _sse(payload) -> bytes:
    return b"data: " + json.dumps(payload).encode()


def test_linha_sse_do_evento_completed():
    out = usage_from_sse_line(_sse(_completed_event()))
    assert out["total_tokens"] == 1540


def test_linha_grande_nao_e_truncada():
    # o evento completed carrega o texto gerado inteiro: era exatamente o que
    # estourava a janela de 16 KiB do agent e matava o parse
    linha = _sse(_completed_event(text="x" * 200_000))
    assert len(linha) > 16384
    assert usage_from_sse_line(linha)["prompt_tokens"] == 1200


def test_ruido_do_stream_e_ignorado():
    assert usage_from_sse_line(b"data: [DONE]") is None
    assert usage_from_sse_line(b"") is None
    assert usage_from_sse_line(b": keep-alive") is None
    assert usage_from_sse_line(b'{"usage": {"input_tokens": 1}}') is None  # sem "data:"
    assert usage_from_sse_line(b'data: {"usage": quebrado') is None
    assert usage_from_sse_line(_sse({"type": "response.output_text.delta", "delta": "oi"})) is None


def test_linha_com_cr_do_sse():
    # SSE permite CRLF; o strip tem que cair antes do json.loads
    assert usage_from_sse_line(_sse(_completed_event()) + b"\r") is not None


# ---------- SseUsageScanner ----------


def _responses_stream(text="resposta gerada") -> bytes:
    """Stream do protocolo Responses como o vLLM emite: eventos com usage nulo
    antes, snapshot completo (com a contagem) no fim."""
    eventos = [
        _completed_event(usage=None, event_type="response.created"),
        _completed_event(usage=None, event_type="response.in_progress"),
        {"type": "response.output_text.delta", "delta": text},
        _completed_event(text=text),
    ]
    return b"".join(_sse(e) + b"\n\n" for e in eventos) + b"data: [DONE]\n\n"


def _feed_em_pedacos(stream: bytes, tamanho: int) -> dict | None:
    scanner = SseUsageScanner()
    for i in range(0, len(stream), tamanho):
        scanner.feed(stream[i : i + tamanho])
    return scanner.finish()


def test_scanner_acha_usage_do_responses():
    assert _feed_em_pedacos(_responses_stream(), 4096)["prompt_tokens"] == 1200


def test_scanner_independe_do_corte_dos_chunks():
    # o bug original: chunk da rede não respeita fronteira de linha, e o evento
    # final é grande o bastante pra ser partido em vários. Pedaços de 1 byte é o
    # pior caso possível dessa fragmentação.
    stream = _responses_stream("x" * 20_000)
    for tamanho in (1, 7, 512, 8192, len(stream)):
        out = _feed_em_pedacos(stream, tamanho)
        assert out is not None, f"perdeu o usage com chunk de {tamanho} byte(s)"
        assert out["prompt_tokens"] == 1200


def test_scanner_pega_evento_final_sem_newline():
    stream = _responses_stream().rstrip(b"\n")
    assert _feed_em_pedacos(stream, 64)["completion_tokens"] == 340


def test_scanner_sem_usage_nenhum():
    scanner = SseUsageScanner()
    scanner.feed(_sse({"type": "response.output_text.delta", "delta": "oi"}) + b"\n")
    scanner.feed(b"data: [DONE]\n")
    assert scanner.finish() is None


def test_scanner_finish_e_idempotente():
    scanner = SseUsageScanner()
    scanner.feed(_responses_stream())
    primeiro = scanner.finish()
    assert scanner.finish() == primeiro


def test_scanner_no_stream_de_chat():
    # regressão: o formato que já funcionava antes do fix
    stream = (
        _sse({"choices": [{"delta": {"content": "oi"}}]}) + b"\n\n"
        + _sse({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4}}) + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    out = _feed_em_pedacos(stream, 3)
    assert (out["prompt_tokens"], out["completion_tokens"], out["total_tokens"]) == (12, 4, 16)
