"""Testes de build_usage_metric_rows: a montagem das linhas de usage_metrics
por coleta, e o que acontece com uma chave que o agent ainda contabiliza mas
que já não existe em api_keys. Rodar de docker/gateway/:

    SUPABASE_URL=x SUPABASE_SERVICE_ROLE_KEY=y python3 -m pytest test_usage_collector.py

Mesmas env vars e mesmo motivo de test_sampling_defaults.py (main.py as lê
incondicionalmente no import). Nada aqui toca agent ou Supabase.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from main import build_usage_metric_rows

PER_KEY = {
    "key-a": {"requests": 3, "tokens_in": 100, "tokens_out": 40},
    "key-b": {"requests": 1, "tokens_in": 10, "tokens_out": 5},
}


def _rows(stack_by_key):
    return build_usage_metric_rows(
        machine_id="m1",
        window_start="2026-09-12T00:00:00+00:00",
        per_key=PER_KEY,
        stack_by_key=stack_by_key,
        concurrent_peak=2,
    )


def test_known_keys_keep_id_and_stack():
    rows = _rows({"key-a": "stack-1", "key-b": None})
    by_key = {r["api_key_id"]: r for r in rows}
    assert set(by_key) == {"key-a", "key-b"}
    assert by_key["key-a"]["stack_id"] == "stack-1"
    # Órfã (existe, sem stack): mantém o id, stack nula — comportamento antigo.
    assert by_key["key-b"]["stack_id"] is None
    assert by_key["key-a"]["tokens_in"] == 100
    assert by_key["key-a"]["concurrent_peak"] == 2
    assert all(r["machine_id"] == "m1" for r in rows)


def test_deleted_key_is_written_without_id_but_not_dropped():
    # key-b saiu de api_keys entre o tráfego e a coleta (painel do cliente
    # apagou). Antes, a linha ia com o id e a FK derrubava o POST inteiro —
    # inclusive a linha de key-a, cujos contadores o agent já tinha zerado.
    rows = _rows({"key-a": "stack-1"})
    assert len(rows) == 2
    orphan = next(r for r in rows if r["api_key_id"] is None)
    assert orphan["tokens_in"] == 10
    assert orphan["tokens_out"] == 5
    assert orphan["stack_id"] is None
    kept = next(r for r in rows if r["api_key_id"] == "key-a")
    assert kept["stack_id"] == "stack-1"


def test_lookup_failure_keeps_every_id():
    # None = a resolução falhou, não "nenhuma chave existe": sem informação,
    # nada é anulado (senão uma queda do Supabase apagaria a atribuição de
    # toda a janela).
    rows = _rows(None)
    assert {r["api_key_id"] for r in rows} == {"key-a", "key-b"}
    assert all(r["stack_id"] is None for r in rows)
