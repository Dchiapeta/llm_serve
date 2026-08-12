"""Testes da tradução Anthropic <-> OpenAI, com foco no filtro de raciocínio.

    python3 -m pytest test_anthropic_compat.py

O que importa aqui é o par com main.py:filtered_reasoning_stream: são duas
cópias do mesmo filtro, uma por protocolo, e o Claude Code passa SÓ por esta.
"""

import asyncio
import json

import httpx

from anthropic_compat import anthropic_sse_from_openai_stream, openai_to_anthropic_response


class FakeUpstream:
    """Mínimo que o conversor consome do httpx.Response em modo stream.

    `stall_after` faz o stream PARAR de produzir depois de N chunks, sem
    fechar — é assim que um pod em prefill longo (ou travado) se comporta, e o
    caso que o watchdog de TTFT/idle existe pra pegar. `raise_after` simula a
    conexão caindo de verdade."""

    def __init__(self, chunks: list[bytes], stall_after: int | None = None,
                 raise_after: int | None = None):
        self._chunks = chunks
        self._stall_after = stall_after
        self._raise_after = raise_after
        self.closed = False

    async def aiter_bytes(self):
        for i, c in enumerate(self._chunks):
            if self._stall_after is not None and i >= self._stall_after:
                await asyncio.Event().wait()  # nunca resolve
            if self._raise_after is not None and i >= self._raise_after:
                raise httpx.ReadError("conexão morreu")
            yield c
        if self._stall_after == len(self._chunks):
            await asyncio.Event().wait()
        if self._raise_after == len(self._chunks):
            raise httpx.ReadError("conexão morreu")

    async def aclose(self):
        self.closed = True


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
    chunks: list[bytes], *, filter_reasoning: bool, input_tokens_estimate: int = 0,
    upstream=None, **kwargs
) -> list[dict]:
    """Roda o conversor e devolve os eventos SSE já decodificados."""

    async def run():
        out = []
        gen = anthropic_sse_from_openai_stream(
            upstream if upstream is not None else FakeUpstream(chunks),
            "claude-x", filter_reasoning=filter_reasoning,
            input_tokens_estimate=input_tokens_estimate, **kwargs,
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


def test_message_delta_leva_os_quatro_campos_de_usage():
    """Mesma razão do message_start: cliente que soma input + cache_* recebe
    NaN quando um campo vem undefined, e NaN nunca ultrapassa o limiar de
    compactação — o auto-compact simplesmente nunca dispara."""
    usage = _only(_collect([_chunk(content="oi")], filter_reasoning=False), "message_delta")["usage"]
    assert set(usage) == {
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    }


def test_resposta_nao_streaming_leva_os_quatro_campos_de_usage():
    usage = openai_to_anthropic_response(
        {"choices": [{"message": {"content": "oi"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        "claude-x",
    )["usage"]
    assert usage["input_tokens"] == 10
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


# ---------- stream que morre: nunca fingir sucesso ----------


def _types(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def test_prefill_estourado_emite_error_e_nao_mensagem_vazia():
    """A REGRESSÃO CENTRAL. Um upstream que nunca manda o primeiro chunk (pod
    em prefill de ~120k tokens) fazia o gerador emitir message_delta com
    stop_reason "end_turn" e message_stop, sob HTTP 200 — um turno vazio que
    AFIRMA sucesso. Era isso que fazia o /compact do Claude Code "travar sem
    retornar nada": ele recebia um resumo vazio e válido, e a conversa seguia
    crescendo até estourar a janela."""
    events = _collect(
        [], filter_reasoning=False, upstream=FakeUpstream([], stall_after=0),
        ttft_timeout_s=0.05, ping_interval_s=0.01,
    )
    assert "error" in [e.get("type") for e in events]
    assert "message_stop" not in _types(events)
    assert "message_delta" not in _types(events)


def test_ping_mantem_a_conexao_viva_durante_o_prefill():
    """Enquanto o vLLM faz prefill não há nada de útil a mandar, mas a conexão
    precisa continuar viva (Cloudflare nos dois lados do caminho)."""
    events = _collect(
        [], filter_reasoning=False, upstream=FakeUpstream([], stall_after=0),
        ttft_timeout_s=0.2, ping_interval_s=0.01,
    )
    assert _types(events).count("ping") >= 2
    # o ping vem DEPOIS do message_start e ANTES de qualquer conteúdo
    assert _types(events)[0] == "message_start"


def test_silencio_no_meio_usa_a_janela_curta_e_entrega_o_parcial():
    """Depois do primeiro chunk o prazo é o de silêncio (curto), não o de
    prefill — se a janela grande vazasse para esta fase, um pod travado
    seguraria a conexão por minutos. E o texto já gerado tem que chegar."""
    events = _collect(
        [], filter_reasoning=False,
        upstream=FakeUpstream([_chunk(content="parcial")], stall_after=1),
        ttft_timeout_s=30.0, idle_timeout_s=0.05, ping_interval_s=0.01,
    )
    assert _texts(events) == ["parcial"]
    # houve conteúdo: fecha a mensagem em vez de emitir error, mas sem chamar
    # de "end_turn" uma resposta que foi cortada
    assert _only(events, "message_delta")["delta"]["stop_reason"] == "max_tokens"
    assert "message_stop" in _types(events)


def test_upstream_e_sempre_fechado():
    """O httpx só devolve a conexão ao pool quando o stream é consumido até o
    fim; abandonar no meio sem fechar queima um slot de max_connections."""
    for kwargs in (
        {"chunks": []},                                    # fim normal
        {"chunks": [], "stall_after": 0},                  # timeout de prefill
        {"chunks": [_chunk(content="x")], "raise_after": 1},  # conexão caiu
    ):
        upstream = FakeUpstream(**kwargs)
        _collect(
            [], filter_reasoning=False, upstream=upstream,
            ttft_timeout_s=0.05, idle_timeout_s=0.05, ping_interval_s=0.01,
        )
        assert upstream.closed, kwargs


def test_cliente_que_desconecta_no_prefill_nao_deixa_task_orfa():
    """GeneratorExit no meio da espera: a task que lê o upstream tem que morrer
    junto, senão fica rodando sozinha segurando a conexão."""

    async def run():
        upstream = FakeUpstream([], stall_after=0)
        gen = anthropic_sse_from_openai_stream(
            upstream, "claude-x", ttft_timeout_s=30.0, ping_interval_s=0.01,
        )
        await gen.__anext__()  # message_start
        await gen.__anext__()  # primeiro ping, já dentro da espera
        await gen.aclose()     # cliente foi embora
        assert upstream.closed
        # nenhuma task de leitura sobrevive à saída do gerador (o cancel só é
        # efetivado no ciclo seguinte do loop, daí o sleep(0))
        await asyncio.sleep(0)
        vivas = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert vivas == []

    asyncio.run(run())


def test_conexao_que_cai_sem_nada_gerado_tambem_vira_error():
    events = _collect(
        [], filter_reasoning=False, upstream=FakeUpstream([], raise_after=0),
        ttft_timeout_s=30.0, ping_interval_s=0.01,
    )
    assert "error" in _types(events)
    assert "message_stop" not in _types(events)


def test_stream_normal_nao_regride_com_os_watchdogs_ligados():
    """Chunks que chegam dentro dos prazos seguem exatamente como antes."""
    events = _collect(
        [_chunk(content="Ola"), _chunk(content=" mundo"),
         _usage_chunk(prompt_tokens=10, completion_tokens=2)],
        filter_reasoning=False, ttft_timeout_s=30.0, idle_timeout_s=30.0,
        ping_interval_s=5.0,
    )
    assert _texts(events) == ["Ola", " mundo"]
    assert _only(events, "message_delta")["delta"]["stop_reason"] == "end_turn"
    assert "message_stop" in _types(events)
    assert "error" not in _types(events)


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
