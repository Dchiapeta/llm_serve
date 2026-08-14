"""Testes do watchdog de duas janelas dos streams do pod.

    python3 -m pytest test_stream_watchdog.py

O caso que importa é o par (prefill lento, silêncio no meio): eles medem coisas
diferentes e o read único do httpx confundia as duas — foi assim que prompt
grande virou timeout de 60s com o stream morto sendo registrado como 200.

Os tetos aqui são de milissegundos, não os 90s/60s de produção: o que se testa
é a MECÂNICA das duas janelas, não os valores calibrados.
"""

import asyncio

import pytest

from stream_watchdog import UpstreamStreamTimeout, aiter_bytes_watchdog


class FakeUpstream:
    """Mínimo que o watchdog consome de um httpx.Response em modo stream.

    `delays` é o tempo de espera ANTES de cada chunk — é como se modela
    prefill (espera antes do primeiro) e silêncio no meio (espera entre dois).
    Espera None trava para sempre, que é o pod que nunca responde."""

    def __init__(self, chunks: list[bytes], delays: list[float | None] | None = None):
        self._chunks = chunks
        self._delays = delays or [0.0] * len(chunks)
        self.closed = False

    async def aiter_bytes(self):
        for chunk, delay in zip(self._chunks, self._delays):
            if delay is None:
                await asyncio.Event().wait()  # nunca resolve
            elif delay:
                await asyncio.sleep(delay)
            yield chunk

    async def aclose(self):
        self.closed = True


async def _drain(upstream, *, ttft_s, idle_s):
    return [c async for c in aiter_bytes_watchdog(
        upstream, ttft_s=ttft_s, idle_s=idle_s, log_label="teste")]


def test_caminho_feliz_repassa_tudo_em_ordem():
    up = FakeUpstream([b"a", b"b", b"c"])
    assert asyncio.run(_drain(up, ttft_s=1.0, idle_s=1.0)) == [b"a", b"b", b"c"]


def test_prefill_alem_do_teto_levanta_timeout_de_prefill():
    # nenhum chunk chega: é a request de prompt grande esperando o prefill
    up = FakeUpstream([b"a"], delays=[None])
    with pytest.raises(UpstreamStreamTimeout) as e:
        asyncio.run(_drain(up, ttft_s=0.05, idle_s=5.0))
    assert e.value.phase == "prefill"


def test_silencio_no_meio_levanta_timeout_de_silencio():
    # o stream começou e travou depois — pod travado/conexão zumbi
    up = FakeUpstream([b"a", b"b"], delays=[0.0, None])
    with pytest.raises(UpstreamStreamTimeout) as e:
        asyncio.run(_drain(up, ttft_s=5.0, idle_s=0.05))
    assert e.value.phase == "silêncio"


def test_prefill_lento_mas_dentro_do_teto_nao_e_cortado():
    """A regressão que mais importa: prefill legítimo de prompt grande.

    Um teto de TTFT generoso não pode ser encurtado pelo teto de idle, que é
    menor — se as duas janelas forem confundidas de novo, este teste quebra."""
    up = FakeUpstream([b"a", b"b"], delays=[0.20, 0.0])
    assert asyncio.run(_drain(up, ttft_s=1.0, idle_s=0.05)) == [b"a", b"b"]


def test_idle_curto_so_vale_depois_do_primeiro_chunk():
    # espelho do anterior: o mesmo idle curto DEVE cortar se o silêncio vier
    # depois do primeiro chunk
    up = FakeUpstream([b"a", b"b"], delays=[0.20, 0.30])
    with pytest.raises(UpstreamStreamTimeout) as e:
        asyncio.run(_drain(up, ttft_s=1.0, idle_s=0.05))
    assert e.value.phase == "silêncio"


def test_teto_zero_desliga_o_watchdog():
    """Request não-streamed: o primeiro chunk só vem quando a geração fecha,
    então o teto de TTFT tem que sair do caminho (quem manda lá é o read do
    httpx). Com zero, uma espera longa passa."""
    up = FakeUpstream([b"a"], delays=[0.20])
    assert asyncio.run(_drain(up, ttft_s=0, idle_s=0)) == [b"a"]


def test_stream_vazio_termina_sem_erro():
    up = FakeUpstream([])
    assert asyncio.run(_drain(up, ttft_s=1.0, idle_s=1.0)) == []


def test_timeout_carrega_o_que_esperou_e_o_teto():
    up = FakeUpstream([b"a"], delays=[None])
    with pytest.raises(UpstreamStreamTimeout) as e:
        asyncio.run(_drain(up, ttft_s=0.05, idle_s=5.0))
    assert e.value.budget == 0.05
    assert e.value.waited >= 0.05
    # a mensagem vai pro log do gateway; tem que dizer as duas coisas
    assert "prefill" in str(e.value)


def test_consumidor_cancelado_nao_deixa_leitura_orfa():
    """A request morre (servidor cancela a task) ENQUANTO o watchdog espera um
    chunk. A leitura pendurada tem que ser cancelada junto, senão fica órfã
    segurando a conexão upstream.

    É esse o cenário que o `finally` protege — e não o cliente fechando o
    gerador, que só acontece com ele parado num yield e sem leitura pendente.

    Checa a task de LEITURA especificamente (a que roda `_next_chunk`): olhar
    `all_tasks` inteiro pega também a máquina de teardown do próprio gerador."""
    async def scenario():
        up = FakeUpstream([b"a"], delays=[None])  # trava antes do 1º chunk

        async def consumir():
            async for _ in aiter_bytes_watchdog(
                up, ttft_s=5.0, idle_s=5.0, log_label="teste"
            ):
                pass

        task = asyncio.ensure_future(consumir())
        await asyncio.sleep(0.05)  # deixa o watchdog chegar no asyncio.wait

        def leituras():
            return [t for t in asyncio.all_tasks()
                    if "_next_chunk" in str(t.get_coro())]

        assert leituras(), "a leitura do 1º chunk deveria estar pendente"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)  # deixa o cancelamento propagar

        vivas = [t for t in leituras() if not (t.cancelled() or t.done())]
        assert not vivas, vivas

    asyncio.run(scenario())
