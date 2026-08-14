"""Testes de INTEGRAÇÃO da costura: watchdog + filtro + falha + status do log.

    python3 -m pytest test_filtered_stream_seam.py

Estes são os caminhos que menos rodam em produção e mais custam quando estão
errados: timeout de prefill, queda de conexão no meio, e resposta que termina
sem nada. Enquanto `filtered_reasoning_stream` morava no main.py nenhum deles
tinha teste — main.py não importa sem fastapi — e foi assim que um stream morto
ficou meses sendo gravado como 200.

O que se verifica aqui, e que os testes de unidade do filtro não alcançam:
UM erro terminal por stream, correspondente à causa real, e o status LÓGICO
chegando em quem grava o gateway_requests.
"""

import asyncio
import json

import httpx
import pytest

from reasoning_filter import filtered_reasoning_stream


class FakeUpstream:
    """Mínimo que a costura consome de um httpx.Response em modo stream.

    `stall_after` para de produzir sem fechar (pod em prefill longo ou travado);
    `raise_after` derruba a conexão de verdade."""

    def __init__(self, chunks, *, stall_after=None, raise_after=None, status_code=200):
        self._chunks = chunks
        self._stall_after = stall_after
        self._raise_after = raise_after
        self.status_code = status_code
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


def _sse(delta=None, finish_reason=None) -> bytes:
    choice = {"index": 0, "delta": delta or {}}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    return b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\n"


def _rodar(upstream, *, ttft_s=5.0, idle_s=5.0, thinking_esperado=True):
    """Roda a costura até o fim e devolve (bytes, status_code, usage)."""
    capturado = {}

    def on_close(status_code, usage):
        capturado["status"] = status_code
        capturado["usage"] = usage

    async def go():
        out = b""
        async for pedaco in filtered_reasoning_stream(
            upstream, ttft_s=ttft_s, idle_s=idle_s, log_label="teste",
            thinking_esperado=thinking_esperado, on_close=on_close,
        ):
            out += pedaco
        return out

    bruto = asyncio.run(go())
    return bruto, capturado.get("status"), capturado.get("usage")


def _erros(bruto: bytes) -> list[dict]:
    out = []
    for linha in bruto.split(b"\n"):
        linha = linha.strip()
        if not linha.startswith(b"data:"):
            continue
        payload = linha[len(b"data:") :].strip()
        if payload in (b"[DONE]", b""):
            continue
        chunk = json.loads(payload)
        if chunk.get("error"):
            out.append(chunk["error"])
    return out


# ---------- caminho feliz ----------


def test_caminho_feliz_loga_o_status_do_upstream():
    up = FakeUpstream([_sse({"reasoning": "pensando"}), _sse({"content": "oi"}),
                       b"data: [DONE]\n\n"])
    bruto, status, _ = _rodar(up)
    assert b"oi" in bruto
    assert b"pensando" not in bruto
    assert _erros(bruto) == []
    assert status == 200
    assert up.closed, "upstream não foi fechado"


# ---------- timeout de prefill ----------


def test_timeout_de_prefill_emite_UM_erro_e_loga_504():
    """O erro tem que ser o do timeout — e só ele. O filtro não pode somar o
    dele por cima, senão o cliente recebe duas causas contraditórias."""
    up = FakeUpstream([_sse({"content": "nunca chega"})], stall_after=0)
    bruto, status, _ = _rodar(up, ttft_s=0.05)

    erros = _erros(bruto)
    assert len(erros) == 1, f"esperado exatamente 1 erro terminal, veio {erros}"
    assert erros[0]["code"] == "upstream_timeout"
    assert "prefill" in erros[0]["message"]
    assert status == 504, "timeout gravado com status errado — some do gateway_requests"
    assert bruto.rstrip().endswith(b"data: [DONE]")


def test_timeout_no_meio_do_stream_e_silencio_nao_prefill():
    up = FakeUpstream(
        [_sse({"reasoning": "pensando"}), _sse({"content": "comecou"})],
        stall_after=2,
    )
    bruto, status, _ = _rodar(up, ttft_s=5.0, idle_s=0.05)
    erros = _erros(bruto)
    assert len(erros) == 1
    assert "silêncio" in erros[0]["message"]
    assert status == 504
    # o que já tinha sido entregue continua entregue
    assert b"comecou" in bruto


# ---------- queda de conexão ----------


def test_queda_de_conexao_emite_UM_erro_e_loga_502():
    up = FakeUpstream([_sse({"reasoning": "pensando"})], raise_after=1)
    bruto, status, _ = _rodar(up)

    erros = _erros(bruto)
    assert len(erros) == 1, f"esperado exatamente 1 erro terminal, veio {erros}"
    assert erros[0]["code"] == "upstream_disconnect"
    assert status == 502
    assert b"pensando" not in bruto


def test_queda_depois_de_conteudo_preserva_o_que_ja_saiu():
    up = FakeUpstream(
        [_sse({"reasoning": "pensando"}), _sse({"content": "metade"})],
        raise_after=2,
    )
    bruto, status, _ = _rodar(up)
    assert b"metade" in bruto
    assert len(_erros(bruto)) == 1
    assert status == 502


# ---------- resposta vazia ----------


def test_resposta_so_de_raciocinio_loga_502_e_avisa_o_cliente():
    """Stream termina BEM (com [DONE]), mas o modelo gastou o teto pensando."""
    up = FakeUpstream([
        _sse({"reasoning": "pensando muito"}),
        _sse({}, finish_reason="length"),
        b"data: [DONE]\n\n",
    ])
    bruto, status, _ = _rodar(up)

    erros = _erros(bruto)
    assert len(erros) == 1
    assert erros[0]["code"] == "reasoning_only_response"
    assert b"pensando muito" not in bruto
    assert status == 502, "resposta vazia gravada como sucesso"


def test_resposta_vazia_tem_exatamente_um_terminal_o_erro_depois_so_done():
    """Nem finish_reason nem [DONE] podem preceder o erro — o cliente para de
    ler em ambos. E o chunk terminal vazio não pode sair DEPOIS: seriam dois
    sinais terminais contraditórios. Sobra o erro, e só o [DONE] atrás."""
    up = FakeUpstream([
        _sse({"reasoning": "pensando"}),
        _sse({}, finish_reason="length"),
        b"data: [DONE]\n\n",
    ])
    bruto, _, _ = _rodar(up)
    assert b"finish_reason" not in bruto
    assert bruto.index(b"reasoning_only_response") < bruto.index(b"[DONE]")
    # nada de conteúdo entre o erro e o fim
    depois = bruto.split(b"reasoning_only_response", 1)[1]
    assert depois.strip().endswith(b"data: [DONE]")


def test_tool_call_puro_nao_e_resposta_vazia():
    tool = [{"index": 0, "id": "c1", "type": "function",
             "function": {"name": "ler", "arguments": "{}"}}]
    up = FakeUpstream([
        _sse({"reasoning": "vou chamar"}),
        _sse({"tool_calls": tool}),
        _sse({}, finish_reason="tool_calls"),
        b"data: [DONE]\n\n",
    ])
    bruto, status, _ = _rodar(up)
    assert _erros(bruto) == []
    assert status == 200


# ---------- usage e liberação ----------


def test_usage_chega_no_callback_do_log():
    """Sem isso tokens_in/tokens_out ficam null pra sempre nos planos filtrados."""
    up = FakeUpstream([
        _sse({"content": "oi"}),
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n',
        b"data: [DONE]\n\n",
    ])
    _, _, usage = _rodar(up, thinking_esperado=False)
    assert usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_upstream_fecha_e_callback_roda_mesmo_no_timeout():
    """release_flight/log vivem neste callback: não rodar vaza o contador de
    in_flight e trava capacidade da máquina."""
    up = FakeUpstream([_sse({"content": "x"})], stall_after=0)
    _, status, _ = _rodar(up, ttft_s=0.05)
    assert up.closed
    assert status == 504


def test_buffer_de_raciocinio_nao_vaza_quando_a_conexao_cai():
    """Regime sem parser: a conexão cai no meio do <think>. O acumulado é
    raciocínio privado e não pode sair como consolo."""
    up = FakeUpstream([_sse({"content": "raciocinio secreto"})], raise_after=1)
    bruto, status, _ = _rodar(up, thinking_esperado=True)
    assert b"raciocinio secreto" not in bruto
    assert len(_erros(bruto)) == 1
    assert status == 502
