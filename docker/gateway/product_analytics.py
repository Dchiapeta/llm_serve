"""Metadados de produto; nunca conteúdo, chaves, headers ou email.

O ledger continua sendo a fonte. A exportação usa uma outbox persistida,
janelas UTC fechadas e identidade/UUID/timestamp estáveis nos retries.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from uuid import UUID

import httpx

logger = logging.getLogger("gateway.analytics")
INFERENCE_PATHS = frozenset({
    "chat/completions", "completions", "responses", "messages", "embeddings",
    "documents/extract", "images/extract", "images/generations", "images/edits",
})
EVENTS = frozenset({"first_inference_completed", "api_usage_daily"})
ID_FIELDS = frozenset({"account_id", "stack_id"})
COUNT_FIELDS = frozenset({
    "request_count", "success_count", "error_count", "aborted_count",
    "tokens_in", "tokens_out", "tokens_known_count", "duration_known_count",
    "cost_known_count", "stream_count",
})
NUMBER_FIELDS = frozenset({
    "duration_ms", "duration_ms_sum", "duration_ms_p95", "reported_cost_usd_sum",
})
MODEL = re.compile(r"[A-Za-z0-9_.:/-]{1,120}\Z")


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (
        env.get("POSTHOG_GATEWAY_ENVIRONMENT", "disabled") == "production"
        and bool(env.get("POSTHOG_PROJECT_TOKEN"))
    )


def _uuid(value) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def safe_properties(raw: dict) -> dict:
    """Allowlist explícita também no último ponto antes da rede."""
    if not isinstance(raw, dict):
        raise ValueError("analytics properties must be an object")
    out = {"environment": "production"}
    for key, value in raw.items():
        if key in ID_FIELDS and _uuid(value):
            out[key] = _uuid(value)
        elif key in COUNT_FIELDS and type(value) is int and value >= 0:
            out[key] = value
        elif key in NUMBER_FIELDS and type(value) in (int, float):
            if value >= 0 and value < float("inf"):
                out[key] = value
        elif key == "model":
            out[key] = (
                value if isinstance(value, str) and MODEL.fullmatch(value)
                and "://" not in value else "unknown"
            )
        elif key == "path" and isinstance(value, str) and value in INFERENCE_PATHS:
            out[key] = value
        elif key in {"period_start", "period_end"} and isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    out[key] = parsed.isoformat()
            except ValueError:
                pass
        elif key == "$internal_or_test_user" and type(value) is bool:
            out[key] = value
    if "$internal_or_test_user" in out:
        out["$set"] = {"$internal_or_test_user": out["$internal_or_test_user"]}
    return out


def capture_event(row: dict) -> dict:
    """Rejeita envelope inválido em vez de fabricar identidade ou horário."""
    event_id = _uuid(row.get("id"))
    distinct_id = _uuid(row.get("distinct_id"))
    if not event_id or not distinct_id or row.get("event") not in EVENTS:
        raise ValueError("invalid analytics envelope")
    timestamp = datetime.fromisoformat(row["event_timestamp"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("analytics timestamp requires timezone")
    properties = safe_properties(row.get("properties") or {})
    if "account_id" not in properties:
        raise ValueError("analytics account identity missing")
    return {
        "uuid": event_id, "distinct_id": distinct_id, "event": row["event"],
        "timestamp": timestamp.isoformat(), "properties": properties,
    }


class StreamCompletion:
    """Observa apenas sinais de término SSE; nunca guarda conteúdo no ledger.

    Um EOF limpo sem marcador terminal também é truncamento. Um marcador
    recebido seguido de cancelamento não vira conclusão de cliente.
    """
    def __init__(self):
        self.pending = b""
        self.terminal = False
        self.frame_terminal = False
        self.error = False
        self.discarding = False

    MAX_LINE = 4 * 1024 * 1024

    def feed(self, chunk: bytes):
        if self.discarding:
            newline = chunk.find(b"\n")
            if newline < 0:
                return
            chunk = chunk[newline + 1:]
            self.discarding = False
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if len(line) <= self.MAX_LINE:
                self._line(line)
            else:
                self.error = True  # metadado incompleto nunca prova sucesso
        if len(self.pending) > self.MAX_LINE:
            self.pending = b""
            self.discarding = True
            self.error = True

    def _line(self, line: bytes):
        line = line.rstrip(b"\r")
        if not line:
            # Um cliente SSE só recebe o evento quando chega a linha vazia.
            # O header/data terminal pode vir em chunks separados do delimitador.
            self.terminal = self.terminal or self.frame_terminal
            self.frame_terminal = False
            return
        if line.startswith(b"event:"):
            kind = line[6:].strip()
            if kind in {b"error", b"response.failed", b"response.incomplete"}:
                self.error = True
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            self.frame_terminal = True
            return
        # Os deltas quentes não são parseados. Só eventos de término/erro.
        if not any(word in data for word in (
            b'"response.completed"', b'"response.failed"',
            b'"response.incomplete"', b'"message_stop"', b'"error"',
        )):
            return
        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            return
        if not isinstance(parsed, dict):
            return
        kind = parsed.get("type")
        if kind in {"response.completed", "message_stop"}:
            self.frame_terminal = True
        if parsed.get("error") or kind in {"error", "response.failed", "response.incomplete"}:
            self.error = True

    def status(self, status_code: int, exhausted: bool) -> int:
        # EOF não despacha um frame sem delimitador; não transformar um tail
        # truncado em conclusão só por conter a palavra terminal.
        self.pending = b""
        if status_code >= 400:
            return status_code
        return status_code if exhausted and self.terminal and not self.error else 502


async def export_once(supa, client, env=None) -> int:
    env = os.environ if env is None else env
    if not enabled(env):
        return 0
    await supa.prepare_gateway_analytics_exports()
    try:
        limit = int(env.get("POSTHOG_GATEWAY_MONTHLY_EVENT_LIMIT", "200000"))
    except ValueError:
        limit = 200000
    # Orçamento desta integração, compartilhado entre réplicas no banco.
    limit = max(0, min(limit, 200000))
    rows = await supa.claim_gateway_analytics_exports(limit, batch_size=100)
    if not rows:
        return 0
    batch, sent_ids, invalid_ids = [], [], []
    for row in rows:
        try:
            batch.append(capture_event(row))
            sent_ids.append(row["id"])
        except Exception:
            # Uma linha inválida não pode reprovar o lote inteiro para sempre:
            # sai da fila antes do POST e fica no banco para inspeção.
            invalid_ids.append(row.get("id"))
    if invalid_ids:
        await supa.discard_gateway_analytics_exports([_uuid(i) for i in invalid_ids if _uuid(i)])
        logger.warning("analytics export discarded %d invalid envelope(s)", len(invalid_ids))
    if not batch:
        return len(rows)
    host = "https://eu.i.posthog.com" if env.get("POSTHOG_REGION") == "eu" else "https://us.i.posthog.com"
    response = await client.post(
        host + "/batch/",
        json={"api_key": env["POSTHOG_PROJECT_TOKEN"], "batch": batch},
    )
    response.raise_for_status()
    await supa.ack_gateway_analytics_exports(sent_ids)
    return len(rows)


async def export_loop(supa):
    if not enabled():
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                sent = await export_once(supa, client)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Exceções de rede podem carregar URLs/headers/payloads:
                # registrar só a categoria, nunca o objeto da exceção. Qualquer
                # erro inesperado também não pode matar o loop em silêncio.
                logger.warning("analytics export failed (%s); persisted batch will retry",
                    type(e).__name__)
                sent = 0
            await asyncio.sleep(1 if sent else 60)
