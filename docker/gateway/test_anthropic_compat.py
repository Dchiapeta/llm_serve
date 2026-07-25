"""Testes de anthropic_sse_from_openai_stream — cobre especificamente a
troca do buffer que escondia o raciocínio inteiro (nenhum byte chegava ao
cliente até </think>) pela transmissão ao vivo como content block
"thinking". Sem pytest-asyncio (não é dependência do projeto): os testes
são funções síncronas que rodam o gerador async via asyncio.run()."""

import asyncio
import json

from anthropic_compat import anthropic_sse_from_openai_stream


class _FakeUpstream:
    """Substitui o httpx.Response real — só precisa expor aiter_bytes()."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    async def aiter_bytes(self):
        for line in self._lines:
            yield line


def _chunk(content=None, finish_reason=None, usage=None) -> bytes:
    delta = {}
    if content is not None:
        delta["content"] = content
    obj = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        obj["usage"] = usage
    return f"data: {json.dumps(obj)}\n".encode()


def _usage_only_chunk(usage: dict) -> bytes:
    return f"data: {json.dumps({'usage': usage})}\n".encode()


def _run(chunks: list[bytes], filter_reasoning: bool) -> list[tuple[str, dict]]:
    async def _collect():
        upstream = _FakeUpstream(chunks)
        out = []
        async for raw in anthropic_sse_from_openai_stream(upstream, "test-model", filter_reasoning=filter_reasoning):
            text = raw.decode()
            lines = text.strip("\n").split("\n")
            event_type = lines[0].split(": ", 1)[1]
            data = json.loads(lines[1].split(": ", 1)[1])
            out.append((event_type, data))
        return out

    return asyncio.run(_collect())


def test_raciocinio_e_transmitido_ao_vivo_antes_de_fechar_think():
    """O ponto central do fix: a PRIMEIRA parte do raciocínio (antes de
    </think> aparecer) já deve virar um thinking_delta assim que chega —
    não pode esperar o delimitador de fechamento pra emitir algo."""
    chunks = [
        _chunk(content="raciocinando "),
        _chunk(content="mais</think>resposta final", finish_reason="stop"),
        _usage_only_chunk({"completion_tokens": 5}),
    ]
    events = _run(chunks, filter_reasoning=True)

    kinds = [e[0] for e in events]
    assert "message_start" in kinds
    assert "message_stop" in kinds

    thinking_starts = [e for e in events if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "thinking"]
    assert len(thinking_starts) == 1

    thinking_deltas = [
        e[1]["delta"]["thinking"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "thinking_delta"
    ]
    # a concatenação dos deltas de thinking reproduz o texto antes de </think> —
    # e o primeiro delta chega no MESMO chunk que ainda não tinha visto o
    # delimitador de fechamento (prova que não ficou represado esperando).
    assert "".join(thinking_deltas) == "raciocinando mais"
    assert len(thinking_deltas) >= 2  # pelo menos um delta por chunk de entrada

    text_deltas = [
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "text_delta"
    ]
    assert "".join(text_deltas) == "resposta final"

    # thinking block fecha ANTES do text block abrir
    thinking_stop_idx = next(i for i, e in enumerate(events) if e[0] == "content_block_stop")
    text_start_idx = next(i for i, e in enumerate(events) if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "text")
    assert thinking_stop_idx < text_start_idx


def test_delimitador_dividido_entre_dois_chunks_nao_vaza():
    """"</think>" cortado ao meio entre dois chunks SSE — não pode aparecer
    nem no texto do thinking nem no texto visível."""
    chunks = [
        _chunk(content="pensando</thi"),
        _chunk(content="nk>final", finish_reason="stop"),
    ]
    events = _run(chunks, filter_reasoning=True)

    thinking_deltas = [
        e[1]["delta"]["thinking"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "thinking_delta"
    ]
    text_deltas = [
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "text_delta"
    ]
    assert "".join(thinking_deltas) == "pensando"
    assert "".join(text_deltas) == "final"
    assert "</think>" not in "".join(thinking_deltas)
    assert "</think>" not in "".join(text_deltas)


def test_sem_filter_reasoning_nao_cria_bloco_thinking():
    """Planos fora de REASONING_LEAK_PLANS (filter_reasoning=False) devem
    continuar exatamente como antes — texto direto, sem bloco thinking."""
    chunks = [_chunk(content="resposta direta", finish_reason="stop")]
    events = _run(chunks, filter_reasoning=False)

    assert not any(e[0] == "content_block_start" and e[1]["content_block"]["type"] == "thinking" for e in events)
    text_deltas = [
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "text_delta"
    ]
    assert "".join(text_deltas) == "resposta direta"


def test_stream_termina_sem_fechar_think_flush_a_cauda_pendente():
    """Caso raro (~0-5%, ver comentário no código): max_tokens estoura
    antes do modelo fechar </think>. O que já foi gerado já saiu ao vivo;
    só falta soltar a cauda pendente e fechar o bloco — sem duplicar como
    texto (o fallback antigo despejava tudo de novo como "text")."""
    chunks = [
        _chunk(content="pensando bastante sem nunca fechar", finish_reason="length"),
    ]
    events = _run(chunks, filter_reasoning=True)

    thinking_deltas = [
        e[1]["delta"]["thinking"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"].get("type") == "thinking_delta"
    ]
    assert "".join(thinking_deltas) == "pensando bastante sem nunca fechar"

    # nunca deveria ter aberto um bloco de texto visível — nada além do
    # raciocínio foi gerado
    assert not any(e[0] == "content_block_start" and e[1]["content_block"]["type"] == "text" for e in events)

    # o bloco thinking foi fechado corretamente ao final
    thinking_block_index = next(
        e[1]["index"] for e in events if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "thinking"
    )
    stop_events = [e for e in events if e[0] == "content_block_stop"]
    assert any(e[1]["index"] == thinking_block_index for e in stop_events)
