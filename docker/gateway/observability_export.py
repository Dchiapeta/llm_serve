"""Exportador das integrações de observabilidade (aba Observabilidade de
Integrações, no painel do TryStac).

Cada stack pode ligar destinos (Datadog, PostHog, Langfuse…) em
`stack_integrations` (migration 0052 do TryStac). Este loop lê o ledger
`gateway_requests` de cada stack a partir do cursor da integração e manda em
lote para o destino, no formato dele (observability_formats.py). Nada aqui
roda no caminho da requisição: o cliente nunca espera por um destino lento ou
fora do ar, e um destino fora do ar não perde nada — o cursor só anda depois
de um 2xx.

Entrega pelo menos uma vez: se o ack (avanço do cursor) se perder depois de um
envio bom, o lote sai de novo. Os ids enviados são estáveis por linha do
ledger, então os destinos que deduplicam (PostHog por uuid, Langfuse e
LangSmith por id, ClickHouse pelo ReplacingMergeTree) absorvem o reenvio.

Segurança:
- Os segredos chegam cifrados (Fernet, env INTEGRATIONS_ENCRYPTION_KEY, a
  mesma do TryStac) e só existem em memória durante o envio.
- O endereço é da pessoa, e o POST sai da nossa rede: antes de cada envio o
  host é resolvido e qualquer IP que não seja público recusa a entrega. Sem
  seguir redirect, pelo mesmo motivo.
- Logs só com a categoria do erro. A mensagem que vai para `last_error` (que
  a pessoa lê no painel) passa por `describe_response`, que tira os segredos.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

import observability_formats as formats

logger = logging.getLogger("gateway.observability")

ENCRYPTION_ENV = "INTEGRATIONS_ENCRYPTION_KEY"
# Linhas mais novas que isso ainda podem ter vizinhas comitando fora de ordem
# (created_at é o now() do INSERT) — ver stack_integration_pending_requests.
SETTLE_SECONDS = 30
LEASE_SECONDS = 120
# Integrações pegas por tick, e lotes por integração antes de devolver a vez.
CLAIM_LIMIT = 50
BATCHES_PER_LEASE = 5
CONCURRENCY = 8
IDLE_SLEEP_S = 30
BUSY_SLEEP_S = 5
# Backoff depois de falhas seguidas: 30s, 1min, 2min… até 1h.
BACKOFF_BASE_S = 30
BACKOFF_MAX_S = 3600
MAX_ERROR_CHARS = 300


class DeliveryError(Exception):
    """Falha de entrega com mensagem segura para mostrar no painel."""


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(ENCRYPTION_ENV)) and env.get("OBSERVABILITY_EXPORT", "enabled") != "disabled"


class SecretBox:
    def __init__(self, fernet_key: str) -> None:
        try:
            self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
        except Exception as e:
            size = len(fernet_key) if isinstance(fernet_key, (str, bytes)) else 0
            raise ValueError(
                f"{ENCRYPTION_ENV} inválida: tem {size} caracteres, o esperado são 44 "
                f"em base64 url-safe terminando em '=' ({type(e).__name__})"
            ) from e

    def decrypt(self, token: str | None) -> dict:
        if not token:
            return {}
        try:
            values = json.loads(self._fernet.decrypt(token.encode()).decode())
        except (InvalidToken, ValueError) as e:
            raise DeliveryError("credencial ilegível: salve a integração de novo") from e
        if not isinstance(values, dict):
            raise DeliveryError("credencial ilegível: salve a integração de novo")
        return {key: value for key, value in values.items() if isinstance(value, str)}


def backoff_seconds(failures: int) -> int:
    if failures <= 0:
        return 0
    return min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_MAX_S)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def in_backoff(integration: dict, now: datetime) -> bool:
    failures = integration.get("consecutive_failures") or 0
    last_error_at = _parse_ts(integration.get("last_error_at"))
    if not failures or last_error_at is None:
        return False
    return last_error_at + timedelta(seconds=backoff_seconds(failures)) > now


def _scrub(text: str, secrets: dict) -> str:
    for value in secrets.values():
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return text


def _error_text(response: httpx.Response) -> str:
    """O texto do erro, quando o destino responde em JSON (`{"errors": [...]}`
    do Datadog, `{"error": ...}`, `{"message": ...}`, `{"detail": ...}`); o
    corpo cru nos outros casos."""
    try:
        payload = response.json()
    except ValueError:
        return response.text or ""
    if isinstance(payload, dict):
        for key in ("errors", "error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, list):
                value = "; ".join(item if isinstance(item, str) else json.dumps(item) for item in value)
            elif isinstance(value, dict):
                value = value.get("message") or json.dumps(value)
            if isinstance(value, str) and value.strip():
                return value
    return response.text or ""


def describe_response(response: httpx.Response, secrets: dict) -> str:
    """`HTTP 401: <texto do erro>`. O texto ajuda ("invalid API key"), mas
    vem do destino: sem quebras, cortado e sem nenhum segredo nosso."""
    message = f"HTTP {response.status_code}"
    if response.status_code < 500:
        body = re.sub(r"\s+", " ", _error_text(response)).strip()
        if body:
            message += ": " + _scrub(body, secrets)
    return message[:MAX_ERROR_CHARS]


async def _default_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=0)
    return [info[4][0] for info in infos]


async def ensure_public_host(url: str, resolve=_default_resolve) -> None:
    """Recusa destino que resolve para rede privada, loopback, link-local
    etc. O painel já recusa o óbvio; aqui é o IP de verdade, na hora."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise DeliveryError("o endereço precisa ser https")
    try:
        addresses = await resolve(parts.hostname, parts.port or 443)
    except OSError as e:
        raise DeliveryError(f"não foi possível resolver {parts.hostname}") from e
    if not addresses:
        raise DeliveryError(f"não foi possível resolver {parts.hostname}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise DeliveryError(f"{parts.hostname} aponta para um endereço privado")


async def send(client: httpx.AsyncClient, deliveries: list, secrets: dict, resolve=_default_resolve) -> None:
    checked: set[str] = set()
    for delivery in deliveries:
        host = urlsplit(delivery.url).netloc
        if host not in checked:
            await ensure_public_host(delivery.url, resolve)
            checked.add(host)
        try:
            response = await client.post(
                delivery.url, headers=delivery.headers, content=delivery.content,
                params=delivery.params or None,
            )
        except httpx.TimeoutException as e:
            raise DeliveryError("o destino não respondeu a tempo") from e
        except httpx.HTTPError as e:
            raise DeliveryError(f"falha de conexão com o destino ({type(e).__name__})") from e
        if not 200 <= response.status_code < 300:
            raise DeliveryError(describe_response(response, secrets))


async def export_integration(supa, client, box: SecretBox, integration: dict, now: datetime,
                             resolve=_default_resolve) -> int:
    """Até BATCHES_PER_LEASE lotes de uma integração já com lease. Devolve
    quantas linhas foram enviadas."""
    provider = integration["provider"]
    integration_id = integration["id"]
    updated_at = integration["updated_at"]
    secrets: dict = {}
    sent = 0
    cursor_at, cursor_id = integration["cursor_at"], integration.get("cursor_id")
    until = (now - timedelta(seconds=SETTLE_SECONDS)).isoformat()
    limit = formats.MAX_ROWS.get(provider, formats.DEFAULT_MAX_ROWS)
    lease_until = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()

    try:
        secrets = box.decrypt(integration.get("secrets_encrypted"))
        for _ in range(BATCHES_PER_LEASE):
            rows = await supa.stack_integration_pending_requests(
                integration["stack_id"], cursor_at, cursor_id, until, limit,
            )
            if not rows:
                break
            picked = formats.select_rows(rows, integration.get("events") or {})
            if picked:
                deliveries = formats.build_deliveries(
                    provider, integration.get("config") or {}, secrets, integration["stack_id"], picked,
                )
                await send(client, deliveries, secrets, resolve)
                sent += len(picked)
            cursor_at, cursor_id = rows[-1]["created_at"], rows[-1]["id"]
            patch = {"cursor_at": cursor_at, "cursor_id": cursor_id, "lease_until": lease_until}
            if picked:
                patch.update({
                    "last_success_at": datetime.now(timezone.utc).isoformat(),
                    "consecutive_failures": 0,
                })
            if not await supa.update_stack_integration(integration_id, updated_at, patch):
                return sent  # mudou no meio (pausa, edição): larga sem soltar o lease alheio
            if len(rows) < limit:
                break
    except (DeliveryError, formats.ConfigError) as e:
        await _record_failure(supa, integration, _scrub(str(e), secrets)[:MAX_ERROR_CHARS])
        return sent
    except Exception as e:
        # Bug nosso ou o Supabase fora: não é culpa da integração, então não
        # conta como falha dela — o lease expira e a próxima volta tenta.
        logger.warning("observability export %s/%s falhou (%s)", provider, integration_id, type(e).__name__)
        return sent

    await supa.update_stack_integration(integration_id, updated_at, {"lease_until": None})
    return sent


async def _record_failure(supa, integration: dict, message: str) -> None:
    try:
        await supa.update_stack_integration(integration["id"], integration["updated_at"], {
            "last_error": message,
            "last_error_at": datetime.now(timezone.utc).isoformat(),
            "consecutive_failures": (integration.get("consecutive_failures") or 0) + 1,
            "lease_until": None,
        })
    except Exception as e:
        logger.warning("observability export: falha ao registrar erro (%s)", type(e).__name__)


async def export_once(supa, client, box: SecretBox, now: datetime | None = None,
                      resolve=_default_resolve) -> int:
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    until_iso = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
    candidates = await supa.list_exportable_stack_integrations(now_iso, CLAIM_LIMIT)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def run(candidate: dict) -> int:
        if in_backoff(candidate, now):
            return 0
        async with semaphore:
            leased = await supa.lease_stack_integration(
                candidate["id"], candidate["updated_at"], now_iso, until_iso,
            )
            if leased is None:
                return 0
            return await export_integration(supa, client, box, leased, now, resolve)

    results = await asyncio.gather(*(run(c) for c in candidates), return_exceptions=True)
    sent = 0
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("observability export: integração falhou (%s)", type(result).__name__)
        else:
            sent += result
    return sent


async def export_loop(supa):
    if not enabled():
        logger.info("observability export desligado (%s ausente)", ENCRYPTION_ENV)
        return
    try:
        box = SecretBox(os.environ[ENCRYPTION_ENV])
    except ValueError as e:
        logger.error("observability export desligado: %s", e)
        return
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=False) as client:
        while True:
            try:
                sent = await export_once(supa, client, box)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("observability export: tick falhou (%s)", type(e).__name__)
                sent = 0
            await asyncio.sleep(BUSY_SLEEP_S if sent else IDLE_SLEEP_S)
