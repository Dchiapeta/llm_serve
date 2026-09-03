"""Testes da retenção de imagens — sem rede: um duplo de SupaClient em memória."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import image_retention


class FakeSupa:
    """Duplo do SupaClient com o mínimo que o reaper usa.

    Guarda as chamadas para que os testes verifiquem a ORDEM (storage antes do
    banco) e o CONTEÚDO (só os ids confirmados são marcados), que é onde os bugs
    desta rotina moram.
    """

    def __init__(self, rows=None, delete_error=None):
        self.rows = list(rows or [])
        self.delete_error = delete_error
        self.deleted: list[list[str]] = []
        self.marked: list[tuple[list[str], str]] = []
        self.calls: list[str] = []

    async def list_expired_images(self, cutoff_iso, limit):
        self.calls.append("list")
        return self.rows[:limit]

    async def delete_image_objects(self, storage_paths):
        self.calls.append("delete")
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(list(storage_paths))

    async def mark_images_file_deleted(self, ids, ts):
        self.calls.append("mark")
        self.marked.append((list(ids), ts))


def _rows(n):
    return [{"id": f"id-{i}", "storage_path": f"stack/2026-09-03/batch-{i}.png"}
            for i in range(n)]


@pytest.fixture(autouse=True)
def _limpa_trava():
    # a trava é módulo-global: um teste que falhe no meio não pode travar o resto
    image_retention._locks.clear()
    yield
    image_retention._locks.clear()


def run(coro):
    return asyncio.run(coro)


# ---------- caminho feliz ----------


def test_apaga_arquivos_e_marca_as_linhas():
    supa = FakeSupa(_rows(3))
    assert run(image_retention.reap_expired_images_once(supa)) == 3
    assert len(supa.deleted[0]) == 3
    assert sorted(supa.marked[0][0]) == ["id-0", "id-1", "id-2"]


def test_storage_antes_do_banco():
    # inverter a ordem perderia o storage_path e deixaria órfão pago no bucket
    supa = FakeSupa(_rows(2))
    run(image_retention.reap_expired_images_once(supa))
    assert supa.calls == ["list", "delete", "mark"]


def test_nada_expirado_nao_toca_em_nada():
    supa = FakeSupa([])
    assert run(image_retention.reap_expired_images_once(supa)) == 0
    assert supa.deleted == []
    assert supa.marked == []


def test_respeita_o_tamanho_do_lote():
    supa = FakeSupa(_rows(10))
    assert run(image_retention.reap_expired_images_once(supa, batch=4)) == 4


def test_linha_sem_storage_path_nao_quebra_o_ciclo():
    supa = FakeSupa([{"id": "id-0", "storage_path": None}])
    assert run(image_retention.reap_expired_images_once(supa)) == 0
    assert supa.deleted == []


# ---------- falha ----------


def test_falha_no_delete_nao_marca_nada():
    # marcar aqui trocaria um retry barato por arquivo órfão permanente
    supa = FakeSupa(_rows(3), delete_error=RuntimeError("storage fora do ar"))
    assert run(image_retention.reap_expired_images_once(supa)) == 0
    assert supa.marked == []


def test_falha_no_delete_deixa_o_lote_para_o_proximo_ciclo():
    supa = FakeSupa(_rows(2), delete_error=RuntimeError("boom"))
    run(image_retention.reap_expired_images_once(supa))
    supa.delete_error = None
    assert run(image_retention.reap_expired_images_once(supa)) == 2


def test_erro_no_list_propaga_para_o_loop_tratar():
    class Explode(FakeSupa):
        async def list_expired_images(self, cutoff_iso, limit):
            raise RuntimeError("supabase fora do ar")

    with pytest.raises(RuntimeError):
        run(image_retention.reap_expired_images_once(Explode()))


def test_trava_e_liberada_mesmo_com_erro():
    # sem o finally, um erro deixaria o reaper travado até o TTL expirar
    class Explode(FakeSupa):
        async def list_expired_images(self, cutoff_iso, limit):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run(image_retention.reap_expired_images_once(Explode()))
    assert image_retention._locks == {}


# ---------- idempotência e concorrência ----------


def test_segundo_ciclo_nao_remarca_o_que_ja_saiu():
    # o FakeSupa não filtra, mas o ciclo real depende do file_deleted_at is null
    # da query; aqui o que se garante é que o ciclo não duplica trabalho sozinho
    supa = FakeSupa(_rows(2))
    run(image_retention.reap_expired_images_once(supa))
    supa.rows = []
    assert run(image_retention.reap_expired_images_once(supa)) == 0


def test_ciclo_concorrente_e_pulado():
    # o /admin/reap-images não pode rodar em cima do loop automático
    supa = FakeSupa(_rows(2))
    image_retention._locks[image_retention._LOCK_KEY] = image_retention.time.time()
    assert run(image_retention.reap_expired_images_once(supa)) == 0
    assert supa.calls == []


def test_trava_vazada_nao_prende_o_reaper_para_sempre():
    supa = FakeSupa(_rows(1))
    vencida = image_retention.time.time() - image_retention.LOCK_TTL_S - 1
    image_retention._locks[image_retention._LOCK_KEY] = vencida
    assert run(image_retention.reap_expired_images_once(supa)) == 1


# ---------- cutoff ----------


def test_retention_cutoff_olha_para_tras():
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert image_retention.retention_cutoff(now, 30) == now - timedelta(days=30)
