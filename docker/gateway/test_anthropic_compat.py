"""Testes da tradução Anthropic <-> OpenAI, com foco no filtro de raciocínio.

    python3 -m pytest test_anthropic_compat.py

O que importa aqui é o par com main.py:filtered_reasoning_stream: são duas
cópias do mesmo filtro, uma por protocolo, e o Claude Code passa SÓ por esta.
"""

import asyncio
import json

from anthropic_compat import anthropic_sse_from_openai_stream


class FakeUpstream:
    """Mínimo que o conversor consome do httpx.Response em modo stream."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def _chunk(**delta) -> bytes:
    payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _usage_chunk(prompt_tokens: int, completion_tokens: int) -> bytes:
    """Chunk final que o vLLM manda quando stream_options.include_usage está
    ligado (validate_body força isso em toda request streaming)."""
    payload = {
        "choices": [],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _collect(
    chunks: list[bytes], *, filter_reasoning: bool, input_tokens_estimate: int = 0
) -> list[dict]:
    """Roda o conversor e devolve os eventos SSE já decodificados."""

    async def run():
        out = []
        gen = anthropic_sse_from_openai_stream(
            FakeUpstream(chunks), "claude-x", filter_reasoning=filter_reasoning,
            input_tokens_estimate=input_tokens_estimate,
        )
        async for raw in gen:
            for line in raw.decode().split("\n"):
                if line.startswith("data:"):
                    out.append(json.loads(line[5:]))
        return out

    return asyncio.run(run())


def _texts(events: list[dict]) -> list[str]:
    return [
        e["delta"]["text"]
        for e in events
        if e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "text_delta"
    ]


def test_reasoning_content_desliga_o_filtro_e_preserva_streaming():
    """Com --reasoning-parser ligado o raciocínio vem em "reasoning_content" e
    "content" nunca traz </think>. Sem este branch o filtro represaria a
    resposta INTEIRA até o fim do stream (o Claude Code perde streaming)."""
    events = _collect(
        [
            _chunk(reasoning_content="pensando..."),
            _chunk(reasoning_content=" mais um pouco"),
            _chunk(content="Ola"),
            _chunk(content=" mundo"),
        ],
        filter_reasoning=True,
    )
    # dois deltas SEPARADOS = streaming incremental preservado; um só significaria
    # que tudo saiu de uma vez no fallback de fim de stream
    assert _texts(events) == ["Ola", " mundo"]


def test_raciocinio_nao_vaza_quando_chega_em_reasoning_content():
    events = _collect(
        [_chunk(reasoning_content="segredo do raciocinio"), _chunk(content="resposta")],
        filter_reasoning=True,
    )
    assert "".join(_texts(events)) == "resposta"


def test_content_antes_do_primeiro_reasoning_content_nao_e_descartado():
    """Ordem incomum, mas descartar o buffer aqui comeria texto do usuário."""
    events = _collect(
        [
            _chunk(content="inicio"),
            _chunk(reasoning_content="pensando"),
            _chunk(content=" fim"),
        ],
        filter_reasoning=True,
    )
    assert "".join(_texts(events)) == "inicio fim"


def test_sem_parser_o_filtro_de_think_continua_valendo():
    """Template sem ENABLE_REASONING_PARSER: o raciocínio vem cru no "content"
    e só o que vem depois de </think> pode chegar ao cliente."""
    events = _collect(
        [_chunk(content="raciocinio</think>\nresposta"), _chunk(content=" final")],
        filter_reasoning=True,
    )
    assert "".join(_texts(events)) == "resposta final"


def test_sem_think_e_sem_parser_devolve_o_buffer_no_fim():
    """Fallback: bateu o fim do stream sem nunca fechar </think> — melhor
    entregar o acumulado do que sumir com a resposta."""
    events = _collect([_chunk(content="texto sem tag")], filter_reasoning=True)
    assert "".join(_texts(events)) == "texto sem tag"


def test_filtro_desligado_repassa_tudo():
    events = _collect(
        [_chunk(content="a"), _chunk(content="b")], filter_reasoning=False
    )
    assert _texts(events) == ["a", "b"]


def _only(events: list[dict], kind: str) -> dict:
    (event,) = [e for e in events if e.get("type") == kind]
    return event


def test_message_start_leva_a_estimativa_de_input_tokens():
    """O Claude Code rastreia o contexto pelo usage.input_tokens que a gente
    devolve, e em streaming o vLLM só manda usage no chunk FINAL. Zero aqui
    (o comportamento anterior) fazia o contador do cliente nunca sair do lugar
    e o auto-compact nunca disparar."""
    events = _collect([_chunk(content="oi")], filter_reasoning=False, input_tokens_estimate=1234)
    usage = _only(events, "message_start")["message"]["usage"]
    assert usage["input_tokens"] == 1234
    assert usage["output_tokens"] == 0
    # presentes e zerados, não ausentes: há cliente que soma os três
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


def test_message_delta_corrige_a_estimativa_com_a_contagem_do_vllm():
    events = _collect(
        [_chunk(content="oi"), _usage_chunk(prompt_tokens=999, completion_tokens=7)],
        filter_reasoning=False,
        input_tokens_estimate=1234,
    )
    assert _only(events, "message_start")["message"]["usage"]["input_tokens"] == 1234
    usage = _only(events, "message_delta")["usage"]
    assert usage["input_tokens"] == 999  # real do vLLM vence a estimativa
    assert usage["output_tokens"] == 7


def test_message_delta_sem_usage_do_vllm_repete_a_estimativa():
    """Stream cortado ou vLLM sem include_usage: melhor repetir a estimativa
    do que zerar o contador de contexto do cliente."""
    events = _collect([_chunk(content="oi")], filter_reasoning=False, input_tokens_estimate=1234)
    assert _only(events, "message_delta")["usage"]["input_tokens"] == 1234


def test_sem_estimativa_o_input_tokens_fica_zero():
    """Regressão dos chamadores que não passam o parâmetro (proxy genérico)."""
    events = _collect([_chunk(content="oi")], filter_reasoning=False)
    assert _only(events, "message_start")["message"]["usage"]["input_tokens"] == 0


def test_tool_call_depois_de_reasoning_content_abre_o_bloco_certo():
    """O branch novo reescreve `delta`; o tool_calls tem que sobreviver a isso."""
    tool = {
        "index": 0,
        "id": "call_1",
        "function": {"name": "grep", "arguments": '{"q":'},
    }
    events = _collect(
        [_chunk(reasoning_content="pensando", tool_calls=[tool])],
        filter_reasoning=True,
    )
    starts = [e for e in events if e.get("type") == "content_block_start"]
    assert [s["content_block"]["type"] for s in starts] == ["tool_use"]
    assert starts[0]["content_block"]["name"] == "grep"
