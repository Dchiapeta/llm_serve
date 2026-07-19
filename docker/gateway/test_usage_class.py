"""Testes da classificação de consumo (usage_class.py) — módulo puro.

    python3 -m pytest test_usage_class.py
"""

from datetime import datetime, timedelta, timezone

from usage_class import DEFAULT_WEIGHTS, class_weight, classify_stack

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _row(**overrides):
    row = {
        "stack_id": "s1",
        "plan": "VibeCoder",
        "usage_class": "low",
        "usage_class_updated_at": None,
        "max_model_len": 65536,
        "usage_class_config": None,
        "total_tokens": 0,
        "total_requests": 0,
        "active_days": 7,
    }
    row.update(overrides)
    return row


def test_poucos_dias_ativos_nao_classifica():
    row = _row(active_days=2, total_tokens=10_000_000, total_requests=10)
    assert classify_stack(row, NOW) is None


def test_cooldown_bloqueia_reclassificacao():
    row = _row(
        total_tokens=10_000_000,
        total_requests=100,
        usage_class_updated_at=(NOW - timedelta(days=2)).isoformat(),
    )
    assert classify_stack(row, NOW) is None
    # cooldown vencido -> classifica normalmente
    row["usage_class_updated_at"] = (NOW - timedelta(days=8)).isoformat()
    assert classify_stack(row, NOW) is not None


def test_fator_request_relativo_a_janela():
    # 30k tokens/request numa janela de 64k (~46%) -> high;
    # o MESMO padrão numa janela de 200k (15%) -> nada muda (low)
    row = _row(total_tokens=3_000_000, total_requests=100, active_days=14)
    # tokens diários = ~214k/dia, abaixo do limiar diário do VibeCoder (300k)
    assert classify_stack(row, NOW) == "high"
    assert classify_stack(_row(**{**row, "max_model_len": 200_000}), NOW) is None


def test_fator_diario_por_plano():
    # 100 requests pequenos (2k/request numa janela 64k = fator request low)
    # mas 2M tokens/dia -> high pelo fator diário do VibeCoder
    row = _row(total_tokens=14_000_000, total_requests=7_000, active_days=7)
    assert classify_stack(row, NOW) == "high"
    # mesmo consumo diário no Max (limiar high 5M/dia) -> medium
    assert classify_stack(_row(**{**row, "plan": "Max"}), NOW) == "medium"


def test_classe_final_e_o_maior_dos_fatores():
    # request médio (20% da janela) + diário baixo -> medium
    row = _row(total_tokens=1_310_720, total_requests=100, active_days=14)
    assert classify_stack(row, NOW) == "medium"


def test_classe_inalterada_devolve_none():
    row = _row(usage_class="high", total_tokens=10_000_000, total_requests=100)
    assert classify_stack(row, NOW) is None


def test_sem_janela_conhecida_usa_so_o_fator_diario():
    row = _row(
        max_model_len=None, total_tokens=3_000_000, total_requests=10, active_days=14
    )
    # ~214k/dia < 300k -> low; requests gigantes não pesam sem janela
    assert classify_stack(row, NOW) is None


def test_config_do_template_sobrepoe_limiares():
    row = _row(
        total_tokens=3_000_000,
        total_requests=100,
        active_days=14,
        usage_class_config={"req_pct_high": 0.9, "req_pct_medium": 0.8},
    )
    # 46% da janela ficou abaixo dos limiares custom -> low, sem mudança
    assert classify_stack(row, NOW) is None


def test_pesos_default_e_override():
    assert class_weight("low") == DEFAULT_WEIGHTS["low"] == 1.0
    assert class_weight("high") == 3.0
    assert class_weight(None) == 1.0
    assert class_weight("high", {"weights": {"high": 4}}) == 4.0
    assert class_weight("invalida") == 1.0
