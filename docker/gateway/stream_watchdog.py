"""Watchdog de duas janelas para streams SSE vindos do pod (agent/vLLM).

Módulo separado de propósito: main.py não é importável sem fastapi e o resto
das deps de runtime, então nada que viva lá é testável isoladamente. Aqui só
entra asyncio, e a suíte consegue exercitar prefill lento, silêncio no meio do
stream e caminho feliz sem subir o gateway.

O PROBLEMA que isto resolve: o read timeout do httpx é um valor só e não
distingue "ainda estou prefilando" de "travei no meio do stream". Durante o
prefill o pod não emite byte nenhum, então aquele read acaba medindo o prefill
inteiro. Com 60s isso matava justamente a request de prompt grande — e, como o
cabeçalho 200 já tinha ido pro cliente, a morte virava um stream vazio
"bem-sucedido", registrado como sucesso.

Medido no Pro (2× A40, Qwen3.8-27B-FP8) em 14/08/2026: com ~43k tokens de
prompt e concorrência 4/6/8, 18 de 36 requests morreram sem entregar um único
token, todas entre 61,3s e 62,6s, todas gravadas como 200.

O mesmo desenho já existia embutido em anthropic_compat.py (caminho
/v1/messages, que por isso não sofria do bug). Aqui ele fica reutilizável.
"""

import asyncio
import logging
import time

logger = logging.getLogger("gateway.stream")


class UpstreamStreamTimeout(Exception):
    """O upstream ficou além do teto sem mandar byte nenhum.

    `phase` distingue "prefill" (o primeiro chunk nunca veio) de "silêncio" (o
    stream começou e travou no meio). A diferença importa: a primeira é
    esperada em prompt grande e pede teto generoso; a segunda é sintoma de pod
    travado ou conexão zumbi e pede teto curto."""

    def __init__(self, phase: str, waited: float, budget: float):
        super().__init__(
            f"upstream sem chunk por {waited:.0f}s ({phase}, teto {budget:.0f}s)"
        )
        self.phase = phase
        self.waited = waited
        self.budget = budget


async def _next_chunk(iterator):
    """`__anext__` com sentinela em vez de StopAsyncIteration: a exceção não
    sobrevive bem a ser guardada numa Task e reinspecionada."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return None


async def aiter_bytes_watchdog(upstream, *, ttft_s: float, idle_s: float, log_label: str = ""):
    """`upstream.aiter_bytes()` com DUAS janelas em vez do read único do httpx.

    `ttft_s` é o prazo até o PRIMEIRO chunk (o prefill); `idle_s` é o prazo de
    silêncio depois que o stream começou. Zero em qualquer um desliga aquele
    watchdog — útil para request não-streamed, onde o primeiro chunk só chega
    quando a geração inteira fecha e um teto de TTFT cortaria resposta legítima.

    Estourou, levanta UpstreamStreamTimeout. O chamador TEM que tratar isso
    como falha visível: engolir como fim de stream é exatamente o bug que faz
    o cliente receber um [DONE] limpo achando que a resposta veio inteira.

    Pump manual e não `async for`: é o que permite separar os dois prazos.
    NÃO trocar por asyncio.wait_for — ele cancela o `__anext__` no timeout, o
    que injeta CancelledError dentro do gerador do httpx e deixa a conexão
    inutilizável (o chunk seguinte quebra). A task fica pendente de propósito e
    só é cancelada quando já estamos abandonando o stream."""
    chunk_task = None
    try:
        iterator = upstream.aiter_bytes().__aiter__()
        first_chunk_seen = False
        waiting_since = time.monotonic()
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(_next_chunk(iterator))
            budget = idle_s if first_chunk_seen else ttft_s
            done, _ = await asyncio.wait({chunk_task}, timeout=budget or None)
            if not done:
                waited = time.monotonic() - waiting_since
                if budget and waited > budget:
                    chunk_task.cancel()
                    chunk_task = None
                    phase = "silêncio" if first_chunk_seen else "prefill"
                    logger.warning(
                        "stream: %s sem chunk do upstream por %.0fs (teto %.0fs) em %s",
                        phase, waited, budget, log_label or "?",
                    )
                    raise UpstreamStreamTimeout(phase, waited, budget)
                continue
            raw = chunk_task.result()
            chunk_task = None
            if raw is None:
                return
            if not first_chunk_seen:
                first_chunk_seen = True
                logger.info(
                    "stream: TTFT %.1fs em %s",
                    time.monotonic() - waiting_since, log_label or "?",
                )
            waiting_since = time.monotonic()
            yield raw
    finally:
        # o CLIENTE desconectar no meio (GeneratorExit num yield) tem que
        # cancelar a leitura pendente, senão ela fica órfã segurando a conexão
        if chunk_task is not None:
            chunk_task.cancel()
