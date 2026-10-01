"""Testes de request_quota.py — a parte pura da cota mensal de requisições.

O limite e a contagem vêm do banco (stack_request_quota, migration 0076); o
que se protege aqui é a interpretação da resposta e o cache: falha aberta
quando a resposta não faz sentido, e o snapshot vencendo na virada do ciclo.

Rodar: pytest docker/gateway/test_request_quota.py -q
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import request_quota  # noqa: E402
from request_quota import (  # noqa: E402
    QuotaCache,
    QuotaSnapshot,
    quota_body_for,
    quota_headers,
    quota_message,
    snapshot_from_rpc,
    unavailable_snapshot,
)

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc).timestamp()
FIM = "2026-10-18T00:00:00+00:00"


def _row(**over):
    row = {
        "plan": "Go", "quota_limit": 4000, "used": 10,
        "cycle_start": "2026-09-18T00:00:00+00:00", "cycle_end": FIM,
    }
    row.update(over)
    return [row]


# ---------- resposta da RPC ----------


def test_linha_valida_vira_snapshot_com_ttl():
    snap = snapshot_from_rpc(_row(), NOW)
    assert snap.limit == 4000 and snap.used == 10
    assert snap.cycle_end == datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert snap.expires_at == NOW + request_quota.REQUEST_QUOTA_CACHE_TTL_S
    assert not snap.exhausted()


def test_limite_nulo_e_sem_teto():
    # imagem, Max, Enterprise: a 0076 devolve quota_limit nulo
    snap = snapshot_from_rpc(_row(quota_limit=None, used=999_999), NOW)
    assert snap.limit is None and not snap.exhausted()


def test_stack_inexistente_e_sem_teto():
    assert snapshot_from_rpc([], NOW).limit is None
    assert snapshot_from_rpc(None, NOW).limit is None


def test_limite_invalido_falha_aberto():
    for ruim in (0, -1, "4000", True, 3.5):
        assert snapshot_from_rpc(_row(quota_limit=ruim), NOW).limit is None, ruim


def test_postgrest_com_sufixo_z_e_sem_fuso():
    assert snapshot_from_rpc(_row(cycle_end="2026-10-18T00:00:00Z"), NOW).cycle_end.tzinfo
    assert snapshot_from_rpc(_row(cycle_end="2026-10-18T00:00:00"), NOW).cycle_end.tzinfo
    assert snapshot_from_rpc(_row(cycle_end="lixo"), NOW).cycle_end is None


def test_esgota_exatamente_no_limite():
    assert not snapshot_from_rpc(_row(used=3999), NOW).exhausted()
    assert snapshot_from_rpc(_row(used=4000), NOW).exhausted()


def test_indisponivel_libera_e_reconsulta_cedo():
    snap = unavailable_snapshot(NOW)
    assert snap.limit is None and not snap.exhausted()
    assert snap.expires_at == NOW + request_quota.REQUEST_QUOTA_ERROR_TTL_S


# ---------- cache ----------


def test_cache_respeita_ttl():
    cache = QuotaCache()
    snap = snapshot_from_rpc(_row(), NOW)
    cache.put("s1", snap)
    assert cache.get("s1", NOW + 1) is snap
    assert cache.get("s1", snap.expires_at) is None


def test_cache_vence_na_virada_do_ciclo_mesmo_dentro_do_ttl():
    # stack esgotada às 23:59:30 do último dia não pode seguir recusada por
    # mais um TTL depois que a cota renovou
    fim = datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()
    cache = QuotaCache()
    snap = snapshot_from_rpc(_row(used=4000), fim - 30)
    cache.put("s1", snap)
    assert cache.get("s1", fim - 1) is snap
    assert cache.get("s1", fim) is None


def test_cache_sem_ciclo_vale_so_pelo_ttl():
    cache = QuotaCache()
    snap = unavailable_snapshot(NOW)
    cache.put("s1", snap)
    assert cache.get("s1", NOW + 1) is snap


# ---------- recusa ----------


def test_retry_after_ate_a_renovacao():
    snap = snapshot_from_rpc(_row(used=4000), NOW)
    esperado = int(datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp() - NOW)
    assert snap.retry_after_s(NOW) == esperado
    # nunca zero nem negativo
    assert snap.retry_after_s(snap.cycle_end.timestamp() + 5) == 1


def test_headers_da_recusa():
    snap = snapshot_from_rpc(_row(used=4000), NOW)
    h = quota_headers(snap, NOW)
    assert h["x-should-retry"] == "false"
    assert h["X-Quota-Limit"] == "4000" and h["X-Quota-Used"] == "4000"
    assert h["X-Quota-Reset"] == "2026-10-18T00:00:00+00:00"
    assert int(h["Retry-After"]) > 0


def test_mensagem_diz_uso_e_renovacao():
    snap = snapshot_from_rpc(_row(used=4000), NOW)
    msg = quota_message("Go", snap)
    assert "4000/4000" in msg and "2026-10-18" in msg and "plano Go" in msg
    sem_plano = quota_message(None, QuotaSnapshot(limit=1, used=1, cycle_end=None, expires_at=0))
    assert "plano" not in sem_plano and "próximo ciclo" in sem_plano


def test_corpo_no_protocolo_do_cliente():
    assert quota_body_for("openai", "x") == {
        "error": {"message": "x", "type": "quota_exceeded", "code": "quota_exceeded"}
    }
    anth = quota_body_for("anthropic", "x")
    assert anth["type"] == "error" and anth["error"]["type"] == "rate_limit_error"
    # shape desconhecido cai no OpenAI, como error_body_for
    assert "error" in quota_body_for("outro", "x") and "type" not in quota_body_for("outro", "x")
