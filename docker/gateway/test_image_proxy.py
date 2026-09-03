"""Testes do módulo puro image_proxy — sem fastapi, sem httpx, sem rede."""

import asyncio
import os

import pytest

import image_proxy
from image_proxy import UploadTooLarge, counting_stream, unwrap


async def _drain(chunks, ceiling):
    """Consome counting_stream inteiro e devolve o que passou."""
    out = []
    async for c in counting_stream(_aiter(chunks), ceiling):
        out.append(c)
    return out


async def _aiter(items):
    for i in items:
        yield i


def test_stream_abaixo_do_teto_passa_intacto():
    out = asyncio.run(_drain([b"aa", b"bb", b"cc"], ceiling=10))
    assert out == [b"aa", b"bb", b"cc"]


def test_stream_exatamente_no_teto_passa():
    # o corte é `> ceiling`, não `>=`: um corpo do tamanho exato do limite é
    # legítimo, e recusá-lo tornaria o teto anunciado uma mentira de 1 byte
    out = asyncio.run(_drain([b"abcde"], ceiling=5))
    assert out == [b"abcde"]


def test_estoura_levanta_uploadtoolarge():
    with pytest.raises(UploadTooLarge) as e:
        asyncio.run(_drain([b"abc", b"def"], ceiling=5))
    assert e.value.ceiling == 5
    assert e.value.seen == 6


def test_chunk_que_estoura_nao_e_repassado():
    """O corte vem ANTES do yield: o pod nunca recebe corpo truncado."""
    passed = []

    async def run():
        async for c in counting_stream(_aiter([b"aaaa", b"bbbb"]), ceiling=6):
            passed.append(c)

    with pytest.raises(UploadTooLarge):
        asyncio.run(run())
    assert passed == [b"aaaa"]


def test_stream_vazio_nao_estoura():
    assert asyncio.run(_drain([], ceiling=1)) == []


def test_nao_acumula_o_corpo():
    """Guarda contra alguém 'simplificar' counting_stream juntando os chunks.

    O ponto inteiro da função é a memória ficar no tamanho de UM chunk. Um
    corpo de 40 MiB em chunks de 1 MiB tem que passar sem que nada com mais de
    um chunk exista de uma vez — o teste afirma o comportamento observável
    (todos os chunks saem, na ordem) para um volume que denunciaria acúmulo.
    """
    chunk = b"x" * (1024 * 1024)
    out = asyncio.run(_drain([chunk] * 40, ceiling=64 * 1024 * 1024))
    assert len(out) == 40
    assert all(c is chunk for c in out)


# ---------- unwrap ----------


def test_unwrap_acha_excecao_direta():
    exc = UploadTooLarge(10, 11)
    assert unwrap(exc) is exc


def test_unwrap_acha_por_cause():
    """É o caso real: o httpx embrulha a exceção do iterador do corpo."""
    original = UploadTooLarge(10, 11)
    wrapper = RuntimeError("erro de transporte")
    wrapper.__cause__ = original
    assert unwrap(wrapper) is original


def test_unwrap_acha_por_context():
    original = UploadTooLarge(10, 11)
    wrapper = RuntimeError("erro de transporte")
    wrapper.__context__ = original
    assert unwrap(wrapper) is original


def test_unwrap_devolve_none_para_erro_de_rede_real():
    """Sem isto, uma falha genuína de upstream viraria 413 e o cliente ficaria
    reduzindo um corpo que nunca foi o problema."""
    assert unwrap(ConnectionError("pod fora do ar")) is None


def test_unwrap_nao_pendura_em_cadeia_circular():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert unwrap(a) is None


def test_unwrap_desiste_em_cadeia_muito_longa():
    """Teto de profundidade: cadeia funda sem UploadTooLarge devolve None em
    vez de percorrer indefinidamente."""
    head = RuntimeError("0")
    current = head
    for i in range(50):
        nxt = RuntimeError(str(i + 1))
        current.__cause__ = nxt
        current = nxt
    current.__cause__ = UploadTooLarge(1, 2)  # fundo demais pra ser alcançado
    assert unwrap(head) is None


# ---------- tetos ----------


def test_teto_de_generations_e_muito_menor_que_o_de_edits():
    """A assimetria é o ponto: generations é texto, edits carrega arquivos."""
    assert image_proxy.max_generation_bytes() < image_proxy.max_edit_bytes()


def test_teto_de_edits_cobre_o_maximo_que_o_pod_aceita():
    """4 arquivos de 15 MiB (IMAGE_MAX_REFERENCE_IMAGES × IMAGE_MAX_FILE_SIZE_MB)
    mais o overhead do multipart. Um teto abaixo disso recusaria no gateway um
    pedido que o pod aceitaria — o pior lugar pra divergir."""
    assert image_proxy.max_edit_bytes() > 4 * 15 * 1024 * 1024


def test_env_sobrescreve_o_default(monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_EDIT_BYTES", "123")
    assert image_proxy.max_edit_bytes() == 123


def test_env_vazia_cai_no_default(monkeypatch):
    """Variável declarada e vazia no painel do Railway não pode derrubar o
    gateway com ValueError no import — mesmo cuidado do BILLING_GRACE_HOURS."""
    monkeypatch.setenv("MAX_IMAGE_EDIT_BYTES", "")
    assert image_proxy.max_edit_bytes() > 0


def test_env_invalida_cai_no_default(monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_GENERATION_BYTES", "abc")
    assert image_proxy.max_generation_bytes() == 256 * 1024


def test_tetos_sao_lidos_por_chamada_nao_no_import():
    """Funções e não constantes de módulo: os testes acima com monkeypatch só
    funcionam porque a env é lida na chamada. Guarda contra transformar em
    constante de import, que é o que quebraria o override sem redeploy."""
    os.environ.pop("MAX_IMAGE_GENERATION_BYTES", None)
    antes = image_proxy.max_generation_bytes()
    os.environ["MAX_IMAGE_GENERATION_BYTES"] = str(antes + 1)
    try:
        assert image_proxy.max_generation_bytes() == antes + 1
    finally:
        os.environ.pop("MAX_IMAGE_GENERATION_BYTES", None)
