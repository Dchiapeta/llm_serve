"""Cota mensal de requisições por stack (01/10/2026).

O limite de cada plano mora em `plan_request_quotas` (0080), editada pela
página Uso do manager. O número, o
ciclo e a contagem moram no banco (`stack_request_quota`, migration 0076), que
é a mesma regra que o manager lê para mostrar o uso — este módulo não
conhece limite nenhum, só o que a função devolve. Regras do que conta e de
quando o ciclo vira: ver o cabeçalho da 0076.

Leitura com cache por stack (REQUEST_QUOTA_CACHE_TTL_S) e soma local entre
leituras: cada requisição admitida soma 1 no snapshot guardado, então o corte
acontece na requisição exata mesmo com o banco consultado só a cada 60s. É
seguro porque o gateway é réplica única (mesmo motivo de rate_buckets e
key_cache em main.py). Na próxima leitura o banco volta a ser a verdade — a
soma local é só a ponte até a linha de gateway_requests, que é gravada no fim
da requisição, chegar lá.

Falha aberta: se a função não responder (Supabase fora, migration ainda não
aplicada), a requisição passa. Uma cota indisponível não pode derrubar a
inferência de todos os clientes; o erro fica no log e é reconsultado depois de
REQUEST_QUOTA_ERROR_TTL_S.

REQUEST_QUOTA_ENFORCE desligado (o default) só registra no log quem
estouraria — é o modo de conferir a contagem em produção antes de cortar.

Request ponderada (15/10/2026, migration 0079): uma requisição vale
ceil(tokens_in / REQUEST_WEIGHT_TOKENS), mínimo 1 — até 32K tokens de entrada
conta 1, como antes. O corte usa o uso PONDERADO (`used_weighted` da RPC), o
mesmo número que o cliente vê como "requisições usadas" — o peso em si nunca
aparece para ele, então a mensagem de quota_message também não o cita. Os tokens só existem no fim da requisição, então a admissão soma 1 e
log_gateway_request (main.py) soma o resto do peso (`QuotaCache.add`) — a
requisição que atravessa o limite passa, e o corte vale a partir da próxima.

Módulo puro, como cli_policy.py: o I/O (RPC, exceção HTTP) fica em main.py.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# `or` em vez do default de get(): variável declarada e vazia (o .env.example
# copiado sem preencher) tem que cair no default, não em ValueError no import
REQUEST_QUOTA_ENFORCE = _env_flag("REQUEST_QUOTA_ENFORCE")
REQUEST_QUOTA_CACHE_TTL_S = float(os.environ.get("REQUEST_QUOTA_CACHE_TTL_S") or "60")
REQUEST_QUOTA_ERROR_TTL_S = float(os.environ.get("REQUEST_QUOTA_ERROR_TTL_S") or "10")

# Tokens de entrada que valem UMA requisição. Espelha public.request_weight
# (migration 0079) — sem env de propósito: um número diferente aqui e no banco
# faria a soma local e a contagem do ciclo discordarem a cada releitura.
REQUEST_WEIGHT_TOKENS = 32768


def request_weight(tokens_in) -> int:
    """Quantas requisições da cota esta requisição consome. Sem tokens
    conhecidos (erro antes do upstream, rota sem usage) vale 1."""
    if not isinstance(tokens_in, int) or isinstance(tokens_in, bool) or tokens_in <= 0:
        return 1
    return max(1, -(-tokens_in // REQUEST_WEIGHT_TOKENS))


@dataclass
class QuotaSnapshot:
    """Estado da cota de uma stack. `limit` None = sem teto (imagem, Max,
    Enterprise, stack inexistente ou função indisponível)."""

    limit: int | None
    used: int
    cycle_end: datetime | None
    expires_at: float  # time.time() a partir do qual o snapshot é relido

    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def retry_after_s(self, now: float) -> int:
        """Segundos até a cota renovar (mínimo 1, como o Retry-After exige)."""
        if self.cycle_end is None:
            return 3600
        return max(1, int(self.cycle_end.timestamp() - now))


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def snapshot_from_rpc(rows, now: float) -> QuotaSnapshot:
    """Converte a resposta do PostgREST (lista de 0 ou 1 linha) em snapshot.

    Lista vazia = stack não existe (ou foi apagada): sem teto, como plano sem
    entrada. Limite que não é inteiro positivo também — falha aberta pelo mesmo
    motivo do módulo."""
    row = rows[0] if isinstance(rows, list) and rows else {}
    limit = row.get("quota_limit") if isinstance(row, dict) else None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        limit = None
    # used_weighted (0079) é o que corta; sem ela (migration ainda por
    # aplicar) cai no `used` cru, o comportamento da 0077
    used = None
    if isinstance(row, dict):
        used = row.get("used_weighted")
        if not isinstance(used, int) or isinstance(used, bool):
            used = row.get("used")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        used = 0
    cycle_end = _parse_ts(row.get("cycle_end")) if isinstance(row, dict) else None
    return QuotaSnapshot(
        limit=limit, used=used, cycle_end=cycle_end,
        expires_at=now + REQUEST_QUOTA_CACHE_TTL_S,
    )


def unavailable_snapshot(now: float) -> QuotaSnapshot:
    """Snapshot de falha aberta: sem teto até a próxima tentativa."""
    return QuotaSnapshot(
        limit=None, used=0, cycle_end=None, expires_at=now + REQUEST_QUOTA_ERROR_TTL_S,
    )


class QuotaCache:
    """Snapshots por stack_id. Um snapshot vence pelo TTL OU pela virada do
    ciclo — sem o segundo, a stack que esgotou a cota seguiria recusada por até
    um TTL depois de renovar."""

    def __init__(self) -> None:
        self._items: dict[str, QuotaSnapshot] = {}

    def get(self, stack_id: str, now: float) -> QuotaSnapshot | None:
        snap = self._items.get(stack_id)
        if snap is None or snap.expires_at <= now:
            return None
        if snap.cycle_end is not None and snap.cycle_end.timestamp() <= now:
            return None
        return snap

    def put(self, stack_id: str, snap: QuotaSnapshot) -> None:
        self._items[stack_id] = snap

    def add(self, stack_id: str, amount: int) -> None:
        """Soma `amount` no snapshot guardado (o resto do peso de uma
        requisição já admitida). Sem snapshot, não faz nada: a próxima leitura
        do banco já traz a linha de gateway_requests com os tokens."""
        snap = self._items.get(stack_id)
        if snap is not None and amount > 0:
            snap.used += amount

    def clear(self) -> None:
        self._items.clear()


def quota_message(plan: str | None, snap: QuotaSnapshot) -> str:
    plano = f" do plano {plan}" if plan else ""
    renova = (
        f"renova em {snap.cycle_end.strftime('%Y-%m-%d %H:%M UTC')}"
        if snap.cycle_end else "renova no próximo ciclo"
    )
    return f"cota mensal de requisições{plano} esgotada ({snap.used}/{snap.limit}); {renova}"


def quota_headers(snap: QuotaSnapshot, now: float) -> dict[str, str]:
    """Headers da recusa. `x-should-retry: false` porque os SDKs da OpenAI e
    da Anthropic (e o Claude Code, que usa o segundo) reenviam 429 sozinhos, e
    um Retry-After de dias eles ignoram e trocam pelo backoff curto — sem o
    header, cada recusa vira uma rajada de retentativas inúteis."""
    headers = {
        "Retry-After": str(snap.retry_after_s(now)),
        "x-should-retry": "false",
        "X-Quota-Limit": str(snap.limit),
        "X-Quota-Used": str(snap.used),
    }
    if snap.cycle_end is not None:
        headers["X-Quota-Reset"] = snap.cycle_end.isoformat()
    return headers


def openai_quota_body(message: str) -> dict:
    return {"error": {"message": message, "type": "quota_exceeded", "code": "quota_exceeded"}}


def anthropic_quota_body(message: str) -> dict:
    # o `type` da Anthropic é um enum fechado; rate_limit_error é o que o
    # Claude Code reconhece para 429
    return {"type": "error", "error": {"type": "rate_limit_error", "message": message}}


def quota_body_for(shape: str, message: str) -> dict:
    return anthropic_quota_body(message) if shape == "anthropic" else openai_quota_body(message)
