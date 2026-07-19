"""Classificação de stacks por padrão de consumo (low/medium/high).

A classe alimenta a ocupação PONDERADA de máquina (migration 0032): um
usuário de contexto longo (ex.: Claude Code no VibeCoder-64k) consome um
múltiplo do KV de um usuário de chat, então a vaga que ele ocupa também pesa
mais. A classe só influencia alocações FUTURAS (realocação automática,
migração manual, stack nova) — nunca dispara migração forçada.

Dois fatores, classe final = o maior dos dois:
  1. média de tokens por request como FRAÇÃO da janela do plano
     (machines.max_model_len, migration 0031) — fórmula universal, valores
     absolutos derivam por plano; sem janela conhecida o fator é neutro (low).
  2. média de tokens por dia ativo, limiares absolutos POR PLANO (defaults
     abaixo; override em templates.usage_class_config).

Histerese: só classifica com ≥ USAGE_CLASS_MIN_ACTIVE_DAYS dias de atividade
na janela, e respeita cooldown entre mudanças — um dia atípico não pode ficar
migrando o usuário de classe (e de máquina) pra lá e pra cá.

Módulo puro (sem I/O) de propósito: espelha usage_class_weight/limiares do
SQL (0032) e do TS (lib/capacity.ts) — manter os três em sincronia — e é
importável pelos testes sem as env vars do main.py.
"""

from datetime import datetime, timedelta, timezone

CLASS_ORDER = {"low": 0, "medium": 1, "high": 2}

# espelho do usage_class_weight (0032) e stackWeight (lib/capacity.ts):
# low=1.0 preserva a matemática de capacidade anterior à classificação
DEFAULT_WEIGHTS = {"low": 1.0, "medium": 1.5, "high": 3.0}

# fator 1: fração da janela do plano por request
DEFAULT_REQ_PCT_MEDIUM = 0.15
DEFAULT_REQ_PCT_HIGH = 0.40

# fator 2: tokens por dia ativo (medium, high) — por plano. Chutes iniciais
# razoáveis; calibrar com a distribuição real de usage_metrics após ~2
# semanas de VibeCoder-64k (override sem deploy via usage_class_config).
DEFAULT_DAILY_THRESHOLDS = {
    "VibeCoder": (300_000, 1_500_000),
    "Pro": (600_000, 3_000_000),
    "Max": (1_000_000, 5_000_000),
    "Enterprise": (1_000_000, 5_000_000),
}


def class_weight(usage_class: str | None, config: dict | None = None) -> float:
    """Peso de ocupação de uma stack pela classe (espelho do SQL/TS)."""
    klass = usage_class if usage_class in CLASS_ORDER else "low"
    weights = (config or {}).get("weights") or {}
    try:
        return float(weights[klass])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_WEIGHTS[klass]


def _config_float(config: dict | None, key: str, default: float) -> float:
    try:
        return float((config or {})[key])
    except (KeyError, TypeError, ValueError):
        return default


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def classify_stack(
    row: dict,
    now: datetime,
    min_active_days: int = 5,
    cooldown_days: int = 7,
) -> str | None:
    """Classe nova da stack a partir de uma linha do stack_usage_stats
    (RPC da 0032), ou None quando não é para mexer (dados insuficientes,
    cooldown ativo ou classe inalterada)."""
    active_days = row.get("active_days") or 0
    total_requests = row.get("total_requests") or 0
    if active_days < min_active_days or total_requests <= 0:
        return None

    updated_at = _parse_ts(row.get("usage_class_updated_at"))
    if updated_at and now - updated_at < timedelta(days=cooldown_days):
        return None

    config = row.get("usage_class_config")
    total_tokens = row.get("total_tokens") or 0

    # fator 1: fração da janela por request (neutro sem janela conhecida)
    factor_request = "low"
    max_model_len = row.get("max_model_len")
    if isinstance(max_model_len, int) and max_model_len > 0:
        pct = (total_tokens / total_requests) / max_model_len
        if pct > _config_float(config, "req_pct_high", DEFAULT_REQ_PCT_HIGH):
            factor_request = "high"
        elif pct > _config_float(config, "req_pct_medium", DEFAULT_REQ_PCT_MEDIUM):
            factor_request = "medium"

    # fator 2: tokens por dia ativo, limiares do plano
    default_medium, default_high = DEFAULT_DAILY_THRESHOLDS.get(
        row.get("plan"), DEFAULT_DAILY_THRESHOLDS["VibeCoder"]
    )
    daily = total_tokens / active_days
    factor_daily = "low"
    if daily > _config_float(config, "daily_high", default_high):
        factor_daily = "high"
    elif daily > _config_float(config, "daily_medium", default_medium):
        factor_daily = "medium"

    new_class = max(factor_request, factor_daily, key=lambda c: CLASS_ORDER[c])
    current = row.get("usage_class") if row.get("usage_class") in CLASS_ORDER else "low"
    return new_class if new_class != current else None
