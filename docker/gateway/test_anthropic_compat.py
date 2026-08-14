"""Testes da tradução Anthropic <-> OpenAI, com foco no filtro de raciocínio.

    python3 -m pytest test_anthropic_compat.py

O que importa aqui é o par com main.py:filtered_reasoning_stream: são duas
cópias do mesmo filtro, uma por protocolo, e o Claude Code passa SÓ por esta.
"""

import asyncio
import json

import httpx
import pytest

from anthropic_compat import (
    anthropic_nonstreaming_body,
    anthropic_sse_from_openai_stream,
    openai_to_anthropic_response,
)


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


def test_campo_reasoning_novo_desliga_o_filtro_igual_ao_antigo():
    """A 0.24 renomeou "reasoning_content" pra "reasoning". Os testes acima
    cobriam SÓ o nome antigo — ficaram verdes enquanto a produção represava a
    resposta inteira e devolvia turno vazio. Este é o par que faltava."""
    events = _collect(
        [
            _chunk(reasoning="pensando..."),
            _chunk(reasoning=" mais um pouco"),
            _chunk(content="Ola"),
            _chunk(content=" mundo"),
        ],
        filter_reasoning=True,
    )
    assert _texts(events) == ["Ola", " mundo"]


def test_raciocinio_nao_vaza_no_campo_reasoning_novo():
    events = _collect(
        [_chunk(reasoning="segredo do raciocinio"), _chunk(content="resposta")],
        filter_reasoning=True,
    )
    juntos = "".join(_texts(events))
    assert juntos == "resposta"
    assert "segredo" not in json.dumps(events)


def test_so_reasoning_sem_content_vira_erro_e_nao_turno_vazio():
    """O modelo gastou o teto de tokens pensando: nenhum content jamais veio, e
    o stream terminou BEM (sem timeout, sem queda).

    Sem esta cobertura o turno saía vazio com HTTP 200 e stop_reason
    "max_tokens" — o cliente lê como resposta legítima. Nem vazar o raciocínio
    (é privado) nem afirmar sucesso."""
    events = _collect(
        [
            _chunk(reasoning="pensando muito"),
            _chunk(reasoning=" e mais um pouco"),
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n',
            b"data: [DONE]\n\n",
        ],
        filter_reasoning=True,
    )
    bruto = json.dumps(events)
    assert "pensando muito" not in bruto, "vazou raciocínio privado"
    assert _texts(events) == []
    erros = [e for e in events if e.get("type") == "error"]
    assert len(erros) == 1, "turno vazio saiu como sucesso"
    assert "sem texto visível" in erros[0]["error"]["message"]


def test_tool_call_sem_texto_nao_vira_erro_de_resposta_vazia():
    """Resposta só de tool_calls não tem texto — e é resposta legítima. O
    caminho do Claude Code é quase todo tool call; tratar como vazia encheria
    de erro espúrio."""
    tool = {"index": 0, "id": "call_1", "type": "function",
            "function": {"name": "ler", "arguments": "{}"}}
    events = _collect(
        [
            _chunk(reasoning="vou chamar a ferramenta"),
            _chunk(tool_calls=[tool]),
            b"data: [DONE]\n\n",
        ],
        filter_reasoning=True,
    )
    assert [e for e in events if e.get("type") == "error"] == []


def test_sem_parser_o_filtro_de_think_continua_valendo():
    """Template sem ENABLE_REASONING_PARSER: o raciocínio vem cru no "content"
    e só o que vem depois de </think> pode chegar ao cliente."""
    events = _collect(
        [_chunk(content="raciocinio</think>\nresposta"), _chunk(content=" final")],
        filter_reasoning=True,
    )
    assert "".join(_texts(events)) == "resposta final"


def test_buffer_sem_think_close_e_descartado_e_nao_entregue():
    """MUDANÇA DE CONTRATO. Antes, o fim de stream sem </think> entregava o
    buffer acumulado ("melhor resposta estranha que resposta nenhuma").

    Só que com filter_reasoning ligado tudo que vem ANTES do </think> é
    exatamente o raciocínio que o filtro existe pra esconder — o fallback antigo
    trocava resposta vazia por VAZAMENTO. Agora descarta e emite erro terminal.
    """
    events = _collect([_chunk(content="raciocinio privado sem tag")], filter_reasoning=True)
    assert _texts(events) == []
    assert "raciocinio privado" not in json.dumps(events)
    erros = [e for e in events if e.get("type") == "error"]
    assert len(erros) == 1


def test_thinking_desligado_entrega_texto_sem_tags():
    """Com enable_thinking=False (o gateway força quando max_tokens <
    MIN_MAX_TOKENS) não existe raciocínio nenhum: o texto sem </think> é a
    resposta legítima e tem que chegar inteiro.

    É o espelho do teste acima, e a razão de `thinking_esperado` existir —
    descartar aqui comeria toda resposta curta do plano."""
    events = _collect(
        [_chunk(content="resposta curta sem tag")],
        filter_reasoning=True,
        thinking_esperado=False,
    )
    assert "".join(_texts(events)) == "resposta curta sem tag"
    assert [e for e in events if e.get("type") == "error"] == []


def test_thinking_desligado_com_stream_cortado_ainda_entrega():
    """Mesma regra quando o stream cai: sem thinking o texto é resposta."""
    events = _collect(
        [_chunk(content="parcial")],
        filter_reasoning=True,
        thinking_esperado=False,
        upstream=FakeUpstream([_chunk(content="parcial")], raise_after=1),
    )
    assert "".join(_texts(events)) == "parcial"


def test_thinking_desligado_entrega_incremental_desde_o_primeiro_chunk():
    """Dois chunks, DOIS deltas — não um só no fim.

    Par exato do teste do protocolo OpenAI (test_reasoning_filter.py). Com
    thinking desligado não há </think> a esperar, e o estado inicial de
    raciocínio (`in_reasoning = filter_reasoning`, sem olhar o thinking)
    segurava o texto inteiro até o fim do stream: a resposta chegava de uma vez
    só, e nos planos filtrados isso valia pra TODA resposta curta — que é
    justamente quando o gateway desliga o thinking (max_tokens <
    MIN_MAX_TOKENS)."""
    events = _collect(
        [_chunk(content="Ola"), _chunk(content=" mundo")],
        filter_reasoning=True,
        thinking_esperado=False,
    )
    assert _texts(events) == ["Ola", " mundo"], "conteúdo saiu represado"


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


# ---------- não-streaming: o erro do upstream não pode virar turno vazio ----------


def _corpo(raw: bytes, status: int, **kwargs) -> tuple[int, dict, dict | None]:
    status_logico, corpo, usage = anthropic_nonstreaming_body(
        raw, status_code=status, requested_model="claude-x", **kwargs
    )
    return status_logico, json.loads(corpo), usage


@pytest.mark.parametrize("status", [400, 429, 500, 502])
def test_erro_do_upstream_preserva_status_e_mensagem(status):
    """A REGRESSÃO: um corpo de erro do upstream não tem "choices", então a
    tradução devolvia uma message Anthropic válida e VAZIA (content: [],
    stop_reason "end_turn") com o 4xx/5xx por cima. O Claude Code mostrava
    "API Error: 400" e nada mais — a mensagem do vLLM, que costuma dizer
    exatamente o que está errado no prompt, morria aqui."""
    raw = json.dumps(
        {"error": {"message": "prompt is too long: 200000 tokens",
                   "type": "invalid_request_error"}}
    ).encode()
    status_logico, corpo, usage = _corpo(raw, status)

    assert status_logico == status, "status do upstream foi trocado"
    assert corpo["type"] == "error"
    assert corpo["error"]["message"] == "prompt is too long: 200000 tokens"
    assert "content" not in corpo, "corpo de erro passou pela tradução de message"
    assert usage is None


@pytest.mark.parametrize("raw, esperado", [
    (b'{"message":"model not found"}', "model not found"),          # vLLM
    (b'{"detail":"Not Found"}', "Not Found"),                        # FastAPI
    (b'{"error":"sem chave"}', "sem chave"),                         # error string
    (b"upstream indisponivel", "upstream indisponivel"),             # texto puro
])
def test_mensagem_do_erro_sobrevive_a_cada_formato_de_corpo(raw, esperado):
    """Cada camada do caminho erra com um shape diferente; nenhuma pode acabar
    em "erro desconhecido"."""
    _, corpo, _ = _corpo(raw, 400)
    assert corpo["error"]["message"] == esperado


def test_erro_em_html_nao_vaza_para_o_cliente():
    """Página de erro do Cloudflare do RunPod (524). HTML cru não explica nada
    ao usuário e ainda mostra a cara da infra."""
    _, corpo, _ = _corpo(b"<html><title>524 A timeout occurred</title></html>", 524)
    assert corpo["error"]["message"] == "erro do servidor upstream (resposta não-JSON)"


def test_erro_com_corpo_vazio_ainda_diz_alguma_coisa():
    _, corpo, _ = _corpo(b"", 500)
    assert corpo["error"]["message"] == "erro desconhecido do modelo"


def test_resposta_200_normal_continua_traduzida():
    """Espelho: a guarda de erro não pode pegar o caminho feliz junto. Com o
    parser do vLLM ligado, o raciocínio vem em campo próprio e é suprimido; o
    content já vem limpo e vira o bloco de texto."""
    raw = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "oi", "reasoning": "pensei"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }).encode()
    status, corpo, usage = _corpo(raw, 200, filter_reasoning=True)
    assert status == 200
    assert corpo["content"] == [{"type": "text", "text": "oi"}]
    assert corpo["stop_reason"] == "end_turn"
    assert "pensei" not in json.dumps(corpo)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_resposta_200_so_de_raciocinio_vira_erro_502():
    """Mesmo desfecho do streaming: nunca um turno vazio de sucesso."""
    raw = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": None,
                                 "reasoning": "pensei muito"},
                     "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 900},
    }).encode()
    status, corpo, usage = _corpo(raw, 200, filter_reasoning=True)
    assert status == 502
    assert corpo["type"] == "error"
    assert "sem texto visível" in corpo["error"]["message"]
    assert "pensei muito" not in json.dumps(corpo)
    # o usage sobrevive: a request gastou tokens de verdade e precisa ser cobrada
    assert usage == {"prompt_tokens": 10, "completion_tokens": 900}


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
