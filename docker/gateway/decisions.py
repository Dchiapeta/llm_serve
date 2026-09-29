"""Decisões do ciclo de vida de máquina: concedida, negada, e 503 servido
sem criar nada (tabela provision_decisions, migration 0070).

Existe porque metade da pergunta "por que esta máquina subiu?" é "por que a
OUTRA não subiu?" — e até a 0070 os quatro `return False` de
_try_provision_machine_for_plan (interruptor, painel, trava, cooldown) e os
503 pré-rota eram invisíveis: nem gateway_requests (que só grava o que passou
de rota) nem machine_events (que só grava máquina real) os viam.

Puro de propósito — sem fastapi, sem main.py — pelo mesmo motivo de
recovery.py: aqui não há fastapi instalado localmente, e todo teste que faz
`import main` é pulado. O que precisa ser testado mora aqui:

  * provision_gate: a lógica das 4 negações, extraída de main.py;
  * record_bg: gravação fire-and-forget com dedupe;
  * prune_decisions_once: retenção.

Rodar de docker/gateway/:  python3 -m pytest test_trigger_trace.py
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from recovery import lock_active, spawn_tracked

logger = logging.getLogger("gateway.decisions")

OUTCOME_GRANTED = "granted"
OUTCOME_DENIED = "denied"
OUTCOME_SERVED_503 = "served_503"

# Uma rajada de requests em cooldown geraria uma linha por request. Dentro da
# janela, a mesma (cause, alvo) não grava de novo: conta, e a próxima linha
# gravada leva o total em repeat_count. Best-effort em memória — reiniciar o
# gateway zera a janela e a contagem pendente.
DECISION_DEDUP_S = 30.0
_last: dict[tuple[str, str], tuple[float, int]] = {}  # chave -> (ts, suprimidas)

RETENTION_DAYS = 30

# Mesma trava com TTL do image_retention: o loop e um disparo manual não
# podem rodar em cima um do outro.
_LOCK_KEY = "decisions_retention"
LOCK_TTL_S = 300.0
_locks: dict[str, float] = {}


# ---------- Gate de provisionamento ----------

CAUSE_DENIED_SWITCH_OFF = "provision_denied.switch_off"
CAUSE_DENIED_PANEL_UNCONFIGURED = "provision_denied.panel_unconfigured"
CAUSE_DENIED_LOCK_ACTIVE = "provision_denied.lock_active"
CAUSE_DENIED_COOLDOWN = "provision_denied.cooldown"


def provision_gate(
    *,
    switch_on: bool,
    panel_configured: bool,
    lock_active: bool,
    last_attempt: float,
    now: float,
    cooldown_s: float,
    ignore_switch: bool = False,
) -> str | None:
    """Por que NÃO criar agora — ou None quando pode criar.

    A ordem é a de _try_provision_machine_for_plan (main.py) e importa: o
    interruptor vem antes da trava porque desligado é "nunca", e a trava
    antes do cooldown porque "já está criando" é mais informativo do que
    "tentou há pouco". `ignore_switch` é o caminho de REQUEST (cliente
    pagante esperando) — só o interruptor é ignorado, o resto vale."""
    if not ignore_switch and not switch_on:
        return CAUSE_DENIED_SWITCH_OFF
    if not panel_configured:
        return CAUSE_DENIED_PANEL_UNCONFIGURED
    if lock_active:
        return CAUSE_DENIED_LOCK_ACTIVE
    if now - last_attempt < cooldown_s:
        return CAUSE_DENIED_COOLDOWN
    return None


# ---------- Gravação ----------

def _row(outcome: str, cause: str, trigger: dict, repeat_count: int) -> dict:
    """Colunas de provision_decisions a partir do envelope (trigger_ctx.snapshot).
    Os identificadores são promovidos a coluna própria (FK + índice); o
    envelope inteiro vai em trigger_meta pra não perder o resto."""
    return {
        "outcome": outcome,
        "cause": cause,
        "actor": trigger.get("actor") or "gateway",
        "plan": trigger.get("plan"),
        "category": trigger.get("category"),
        "machine_id": trigger.get("machine_id"),
        "account_id": trigger.get("account_id"),
        "stack_id": trigger.get("stack_id"),
        "api_key_id": trigger.get("api_key_id"),
        "key_prefix": trigger.get("key_prefix"),
        "trace_id": trigger.get("trace_id"),
        "trigger_meta": trigger,
        "repeat_count": repeat_count,
    }


def _dedup_key(cause: str, trigger: dict) -> tuple[str, str]:
    # por stack quando há request (cada cliente conta separado); por pool
    # quando é o lifecycle (não há cliente)
    target = trigger.get("stack_id") or f"{trigger.get('plan')}:{trigger.get('category')}"
    return (cause, str(target))


def should_record(cause: str, trigger: dict, now: float | None = None) -> int | None:
    """Decide se esta ocorrência vira linha. None = suprimida (dentro da
    janela); int = repeat_count a gravar (1 + suprimidas desde a última)."""
    now = time.time() if now is None else now
    key = _dedup_key(cause, trigger)
    prev = _last.get(key)
    if prev and now - prev[0] < DECISION_DEDUP_S:
        _last[key] = (prev[0], prev[1] + 1)
        return None
    suppressed = prev[1] if prev else 0
    _last[key] = (now, 0)
    return 1 + suppressed


async def _write(supa, row: dict) -> None:
    try:
        await supa.insert_provision_decision(row)
    except Exception as e:
        # best-effort: diagnóstico nunca derruba o caminho que ele observa
        logger.warning("provision_decisions: falha ao gravar (%s)", e)


def record_bg(supa, outcome: str, cause: str, trigger: dict) -> bool:
    """Grava fire-and-forget. SÍNCRONA de propósito: o chamador em
    _try_provision_machine_for_plan tem a invariante "nenhum await entre a
    checagem e a marcação da trava" — um await aqui reabriria a corrida.

    True = uma linha foi enfileirada; False = suprimida pelo dedupe ou sem
    event loop (testes síncronos)."""
    repeat = should_record(cause, trigger)
    if repeat is None:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False  # sem loop rodando (teste síncrono): nada a enfileirar
    spawn_tracked(_write(supa, _row(outcome, cause, trigger, repeat)))
    return True


# ---------- Retenção ----------

def retention_cutoff(now: datetime, days: int = RETENTION_DAYS) -> datetime:
    return now - timedelta(days=days)


async def prune_decisions_once(supa, *, now: datetime | None = None) -> bool:
    """Apaga decisões mais velhas que RETENTION_DAYS. Idempotente; devolve se
    o ciclo rodou (False = outro ciclo em andamento)."""
    if lock_active(_locks, _LOCK_KEY, LOCK_TTL_S):
        return False
    _locks[_LOCK_KEY] = time.time()
    try:
        now = now or datetime.now(timezone.utc)
        await supa.delete_provision_decisions_before(retention_cutoff(now).isoformat())
        return True
    finally:
        _locks.pop(_LOCK_KEY, None)


async def decisions_retention_loop(supa, interval_s: float = 3600.0) -> None:
    """Mesma forma dos outros loops do gateway: dorme, chama o `_once`, engole
    a exceção — um ciclo que falha é retomado no próximo."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await prune_decisions_once(supa)
        except Exception as e:
            logger.warning("provision_decisions: retenção falhou (%s)", e)
