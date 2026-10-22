"""Formato de cada destino de observabilidade. Puro: recebe as linhas do
ledger e a configuração da integração, devolve as requisições HTTP prontas.
Quem envia, trata erro e avança o cursor é observability_export.py.

Só METADADO — a mesma linha de gateway_requests que a página "Requisições"
mostra: modelo, tokens, latência, status, rota, chave. Nunca prompt ou
resposta (o ledger não guarda texto; ver generation_trace.py).

As linhas chegam de stack_integration_pending_requests (migration 0052 do
TryStac). `created_at` é a gravação do ledger, no fechamento da resposta;
o início da requisição é `created_at - duration_ms`.
"""

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

SERVICE_NAME = "stac"
PROVIDER_NAME = "stac"
CLIENT_NAME = "stac-gateway/1.0"
# namespace dos ids derivados (evento Langfuse, trace Sentry): o mesmo row id
# gera sempre o mesmo id, então um reenvio depois de falha deduplica onde o
# destino deduplica.
ID_NAMESPACE = uuid.UUID("5f1b0c1e-7a8e-4d0e-9a51-2b0f6c9e4d11")

# Rotas que não são inferência e não viram span.
SKIPPED_PATHS = frozenset({"models"})

# gen_ai.operation.name (semconv do OpenTelemetry) por rota do gateway.
OPERATIONS = {
    "chat/completions": "chat",
    "messages": "chat",
    "responses": "chat",
    "completions": "text_completion",
    "embeddings": "embeddings",
}

DATADOG_INTAKE = "https://api.{site}/api/intake/llm-obs/v1/trace/spans"
LANGSMITH_RUNS_BATCH = "https://api.smith.langchain.com/runs/batch"
NEW_RELIC_OTLP = {
    "US": "https://otlp.nr-data.net:4318/v1/traces",
    "EU": "https://otlp.eu01.nr-data.net:4318/v1/traces",
}
CLICKHOUSE_DEFAULT_TABLE = "stac_requests"

# Teto de linhas por lote, por destino. O Sentry recebe uma transação por
# envelope (como os SDKs fazem), então um lote vira N requisições.
MAX_ROWS = {"sentry": 50}
DEFAULT_MAX_ROWS = 500


class ConfigError(ValueError):
    """Configuração salva que não dá para usar (campo faltando, DSN torto).
    A mensagem vai para `last_error`, que a pessoa lê no painel."""


@dataclass
class Delivery:
    url: str
    headers: dict
    content: bytes
    params: dict = field(default_factory=dict)


@dataclass
class Record:
    """Uma linha do ledger já interpretada."""
    id: uuid.UUID
    start: datetime
    end: datetime
    stack_id: str
    api_key_id: str | None
    api_key_name: str | None
    path: str
    model: str | None
    status_code: int
    stream: bool
    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int | None
    user_agent: str | None

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def operation(self) -> str:
        return OPERATIONS.get(self.path, "generate_content")

    @property
    def name(self) -> str:
        return f"{self.operation} {self.model}" if self.model else self.operation

    @property
    def error_message(self) -> str | None:
        if not self.is_error:
            return None
        return "client closed request (499)" if self.status_code == 499 else f"HTTP {self.status_code}"

    @property
    def total_tokens(self) -> int | None:
        if self.tokens_in is None and self.tokens_out is None:
            return None
        return (self.tokens_in or 0) + (self.tokens_out or 0)


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def to_record(row: dict, stack_id: str) -> Record:
    end = datetime.fromisoformat(row["created_at"])
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    duration = _int(row.get("duration_ms"))
    return Record(
        id=uuid.UUID(str(row["id"])),
        start=end - timedelta(milliseconds=duration or 0),
        end=end,
        stack_id=stack_id,
        api_key_id=row.get("api_key_id"),
        api_key_name=row.get("api_key_name"),
        path=str(row.get("path") or ""),
        model=row.get("model"),
        status_code=int(row.get("status_code") or 0),
        stream=bool(row.get("stream")),
        tokens_in=_int(row.get("tokens_in")),
        tokens_out=_int(row.get("tokens_out")),
        duration_ms=duration,
        user_agent=row.get("user_agent"),
    )


def select_rows(rows: list[dict], events: dict) -> list[dict]:
    """Recorte pelos toggles do painel: `requests` são as que deram certo,
    `errors` as que falharam (4xx/5xx/499). Rotas que não são inferência saem."""
    send_ok = events.get("requests") is True
    send_errors = events.get("errors") is True
    picked = []
    for row in rows:
        if row.get("path") in SKIPPED_PATHS:
            continue
        failed = int(row.get("status_code") or 0) >= 400
        if (failed and send_errors) or (not failed and send_ok):
            picked.append(row)
    return picked


def semconv_attributes(r: Record) -> dict:
    """Atributos gen_ai.* do OpenTelemetry, mais os da Stac. Sem None."""
    attrs = {
        "gen_ai.operation.name": r.operation,
        "gen_ai.provider.name": PROVIDER_NAME,
        "gen_ai.system": PROVIDER_NAME,
        "gen_ai.request.model": r.model,
        "gen_ai.response.model": r.model,
        "gen_ai.usage.input_tokens": r.tokens_in,
        "gen_ai.usage.output_tokens": r.tokens_out,
        "http.route": f"/v1/{r.path}",
        "http.response.status_code": r.status_code,
        "user_agent.original": r.user_agent,
        "stac.stack_id": r.stack_id,
        "stac.api_key.id": r.api_key_id,
        "stac.api_key.name": r.api_key_name,
        "stac.stream": r.stream,
    }
    return {key: value for key, value in attrs.items() if value is not None}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns(moment: datetime) -> int:
    # aritmética inteira: float de segundos perde precisão em nanossegundos
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _derived(r: Record, kind: str) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, f"{kind}:{r.id}")


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _require(values: dict, *names: str) -> list[str]:
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise ConfigError("campo obrigatório ausente: " + ", ".join(missing))
    return [values[name] for name in names]


def _json(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


# ---------- OTLP (Grafana Cloud, OpenTelemetry, New Relic) ----------

def _any_value(value) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    return AnyValue(string_value=str(value))


def _kv(attrs: dict) -> list[KeyValue]:
    return [KeyValue(key=key, value=_any_value(value)) for key, value in attrs.items()]


def otlp_traces(records: list[Record]) -> bytes:
    """ExportTraceServiceRequest em protobuf binário — o formato que Grafana
    Cloud e New Relic recomendam (o JSON do OTLP o Grafana só aceita "para
    teste de baixo volume"). Um span SERVER por requisição, raiz do próprio
    trace: o trace_id é o id da linha do ledger."""
    spans = []
    for r in records:
        span = Span(
            trace_id=r.id.bytes,
            span_id=r.id.bytes[8:],
            name=r.name,
            kind=Span.SpanKind.SPAN_KIND_SERVER,
            start_time_unix_nano=_ns(r.start),
            end_time_unix_nano=_ns(r.end),
            attributes=_kv(semconv_attributes(r)),
        )
        if r.is_error:
            span.status.CopyFrom(Status(code=Status.StatusCode.STATUS_CODE_ERROR, message=r.error_message))
        spans.append(span)
    stack_id = records[0].stack_id if records else ""
    request = ExportTraceServiceRequest(resource_spans=[ResourceSpans(
        resource=Resource(attributes=_kv({"service.name": SERVICE_NAME, "stac.stack_id": stack_id})),
        scope_spans=[ScopeSpans(scope=InstrumentationScope(name=CLIENT_NAME), spans=spans)],
    )])
    return request.SerializeToString()


def _traces_url(endpoint: str) -> str:
    """Endpoint base do OTLP/HTTP → caminho de traces. Aceita quem já colou
    a URL completa."""
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/v1/traces") else endpoint + "/v1/traces"


def parse_otlp_headers(value: str) -> dict:
    """`chave=valor,chave=valor` (OTEL_EXPORTER_OTLP_HEADERS). O painel já
    validou; aqui só não deixa passar quebra de linha num header."""
    headers = {}
    for pair in (value or "").split(","):
        if "=" not in pair:
            continue
        name, _, header_value = pair.partition("=")
        name, header_value = name.strip(), header_value.strip()
        if name and header_value and "\n" not in header_value and "\r" not in header_value:
            headers[name] = header_value
    return headers


def _otlp(url: str, headers: dict, records: list[Record]) -> list[Delivery]:
    return [Delivery(url, {**headers, "Content-Type": "application/x-protobuf"}, otlp_traces(records))]


def build_grafana(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    endpoint, instance_id = _require(config, "endpoint", "instanceId")
    (token,) = _require(secrets, "token")
    return _otlp(_traces_url(endpoint), {"Authorization": _basic(instance_id, token)}, records)


def build_opentelemetry(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (endpoint,) = _require(config, "endpoint")
    return _otlp(_traces_url(endpoint), parse_otlp_headers(secrets.get("headers", "")), records)


def build_newrelic(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (license_key,) = _require(secrets, "licenseKey")
    url = NEW_RELIC_OTLP.get(str(config.get("region") or "US").upper())
    if not url:
        raise ConfigError("região do New Relic deve ser US ou EU")
    return _otlp(url, {"api-key": license_key}, records)


# ---------- Datadog LLM Observability ----------

def build_datadog(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (api_key,) = _require(secrets, "apiKey")
    site = str(config.get("site") or "datadoghq.com")
    spans = []
    for r in records:
        meta = {
            "kind": "embedding" if r.operation == "embeddings" else "llm",
            "model_name": r.model or "unknown",
            "model_provider": PROVIDER_NAME,
            "metadata": {"path": f"/v1/{r.path}", "status_code": r.status_code, "stream": r.stream},
        }
        if r.is_error:
            meta["error"] = {"message": r.error_message, "type": "http_error"}
        metrics = {
            key: value for key, value in {
                "input_tokens": r.tokens_in, "output_tokens": r.tokens_out, "total_tokens": r.total_tokens,
            }.items() if value is not None
        }
        tags = [f"stac_stack_id:{r.stack_id}", f"http_status:{r.status_code}"]
        if r.api_key_name:
            tags.append(f"stac_api_key:{r.api_key_name}")
        spans.append({
            "name": r.name,
            "span_id": str(int.from_bytes(r.id.bytes[8:], "big")),
            "trace_id": r.id.hex,
            "parent_id": "undefined",  # raiz: o intake pede a string literal
            "start_ns": _ns(r.start),
            "duration": _ns(r.end) - _ns(r.start),
            "status": "error" if r.is_error else "ok",
            "meta": meta,
            "metrics": metrics,
            "tags": tags,
        })
    payload = {"data": {"type": "span", "attributes": {
        "ml_app": SERVICE_NAME, "tags": ["source:stac"], "spans": spans,
    }}}
    return [Delivery(DATADOG_INTAKE.format(site=site), {
        "DD-API-KEY": api_key, "Content-Type": "application/json",
    }, _json(payload))]


# ---------- PostHog LLM Analytics ----------

def build_posthog(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (token,) = _require(secrets, "projectApiKey")
    host = str(config.get("host") or "https://us.i.posthog.com").rstrip("/")
    batch = []
    for r in records:
        properties = {
            "$ai_trace_id": str(r.id),
            "$ai_span_name": r.name,
            "$ai_model": r.model,
            "$ai_provider": PROVIDER_NAME,
            "$ai_input_tokens": r.tokens_in,
            "$ai_output_tokens": r.tokens_out,
            "$ai_latency": r.duration_ms / 1000 if r.duration_ms is not None else None,
            "$ai_http_status": r.status_code,
            "$ai_is_error": r.is_error,
            "$ai_error": r.error_message,
            "$ai_stream": r.stream,
            "stac_path": f"/v1/{r.path}",
            "stac_stack_id": r.stack_id,
            "stac_api_key_name": r.api_key_name,
            # chave de API não é pessoa: sem perfil, sem cobrar person profile
            "$process_person_profile": False,
        }
        batch.append({
            "event": "$ai_generation",
            "uuid": str(r.id),
            "distinct_id": f"stac_key_{r.api_key_id or r.stack_id}",
            "timestamp": _iso(r.end),
            "properties": {key: value for key, value in properties.items() if value is not None},
        })
    return [Delivery(host + "/batch/", {"Content-Type": "application/json"},
                     _json({"api_key": token, "batch": batch}))]


# ---------- Langfuse ----------

def build_langfuse(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (public_key,) = _require(config, "publicKey")
    (secret_key,) = _require(secrets, "secretKey")
    host = str(config.get("host") or "https://cloud.langfuse.com").rstrip("/")
    batch = []
    for r in records:
        metadata = {
            "path": f"/v1/{r.path}", "status_code": r.status_code, "stream": r.stream,
            "stack_id": r.stack_id, "api_key": r.api_key_name,
        }
        batch.append({
            "id": str(_derived(r, "langfuse-trace")),
            "type": "trace-create",
            "timestamp": _iso(r.end),
            "body": {
                "id": str(r.id), "name": r.name, "timestamp": _iso(r.start),
                "tags": ["stac"], "metadata": metadata,
            },
        })
        usage = {key: value for key, value in {"input": r.tokens_in, "output": r.tokens_out}.items()
                 if value is not None}
        generation = {
            "id": str(_derived(r, "langfuse-generation")), "traceId": str(r.id), "name": r.name,
            "startTime": _iso(r.start), "endTime": _iso(r.end), "model": r.model,
            "level": "ERROR" if r.is_error else "DEFAULT", "metadata": metadata,
        }
        if usage:
            generation["usageDetails"] = usage
        if r.is_error:
            generation["statusMessage"] = r.error_message
        batch.append({
            "id": str(_derived(r, "langfuse-generation-event")),
            "type": "generation-create",
            "timestamp": _iso(r.end),
            "body": generation,
        })
    return [Delivery(host + "/api/public/ingestion", {
        "Authorization": _basic(public_key, secret_key), "Content-Type": "application/json",
    }, _json({"batch": batch}))]


# ---------- LangSmith ----------

def build_langsmith(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (api_key,) = _require(secrets, "apiKey")
    project = str(config.get("project") or "default")
    runs = []
    for r in records:
        usage = {key: value for key, value in {
            "input_tokens": r.tokens_in, "output_tokens": r.tokens_out, "total_tokens": r.total_tokens,
        }.items() if value is not None}
        start = r.start.astimezone(timezone.utc)
        runs.append({
            "id": str(r.id),
            "trace_id": str(r.id),
            "dotted_order": start.strftime("%Y%m%dT%H%M%S%fZ") + str(r.id),
            "name": r.name,
            "run_type": "llm",
            "start_time": _iso(r.start),
            "end_time": _iso(r.end),
            "inputs": {},
            "outputs": {"usage_metadata": usage} if usage else {},
            "error": r.error_message,
            "session_name": project,
            "extra": {"metadata": {
                "ls_provider": PROVIDER_NAME, "ls_model_name": r.model, "path": f"/v1/{r.path}",
                "status_code": r.status_code, "stream": r.stream, "stack_id": r.stack_id,
                "api_key": r.api_key_name,
            }},
        })
    return [Delivery(LANGSMITH_RUNS_BATCH, {"x-api-key": api_key, "Content-Type": "application/json"},
                     _json({"post": runs}))]


# ---------- Sentry ----------

def parse_sentry_dsn(dsn: str) -> tuple[str, str]:
    """DSN → (URL do envelope, public key)."""
    parts = urlsplit(dsn or "")
    project_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
    prefix = parts.path.rstrip("/")[: -len(project_id)].rstrip("/") if project_id else ""
    if parts.scheme != "https" or not parts.username or not parts.hostname or not project_id.isdigit():
        raise ConfigError("DSN do Sentry inválido")
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"https://{host}{prefix}/api/{project_id}/envelope/", parts.username


def build_sentry(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    (dsn,) = _require(secrets, "dsn")
    url, public_key = parse_sentry_dsn(dsn)
    headers = {
        "Content-Type": "application/x-sentry-envelope",
        "X-Sentry-Auth": f"Sentry sentry_version=7, sentry_key={public_key}, sentry_client={CLIENT_NAME}",
    }
    deliveries = []
    for r in records:
        event_id = r.id.hex
        transaction = {
            "event_id": event_id,
            "type": "transaction",
            "platform": "other",
            "transaction": r.name,
            "transaction_info": {"source": "custom"},
            "start_timestamp": r.start.timestamp(),
            "timestamp": r.end.timestamp(),
            "contexts": {"trace": {
                "trace_id": _derived(r, "sentry-trace").hex,
                "span_id": r.id.hex[16:],
                "op": f"gen_ai.{r.operation}",
                "status": "internal_error" if r.is_error else "ok",
                "data": semconv_attributes(r),
            }},
            "tags": {"stac.path": f"/v1/{r.path}", "http.status_code": str(r.status_code),
                     **({"gen_ai.request.model": r.model} if r.model else {})},
            "spans": [],
        }
        envelope = b"\n".join([
            _json({"event_id": event_id, "sent_at": _iso(datetime.now(timezone.utc))}),
            _json({"type": "transaction"}),
            _json(transaction),
        ]) + b"\n"
        deliveries.append(Delivery(url, headers, envelope))
    return deliveries


# ---------- ClickHouse ----------

def _clickhouse_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def clickhouse_table(config: dict) -> str:
    """`database.tabela`. Os dois já chegam validados como identificador
    simples pelo painel; a checagem se repete porque entram no SQL."""
    database = str(config.get("database") or "default")
    table = str(config.get("table") or CLICKHOUSE_DEFAULT_TABLE)
    for name in (database, table):
        if not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise ConfigError("nome de banco ou tabela do ClickHouse inválido")
    return f"`{database}`.`{table}`"


def build_clickhouse(config: dict, secrets: dict, records: list[Record]) -> list[Delivery]:
    url, username = _require(config, "url", "username")
    (password,) = _require(secrets, "password")
    table = clickhouse_table(config)
    headers = {"X-ClickHouse-User": username, "X-ClickHouse-Key": password}
    # Cria se não existir, a cada lote: barato, e a pessoa não precisa rodar
    # DDL antes de conectar. ReplacingMergeTree por id deduplica os reenvios.
    create = (
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "id UUID, created_at DateTime64(3, 'UTC'), started_at DateTime64(3, 'UTC'), "
        "stack_id UUID, api_key_id Nullable(UUID), api_key_name Nullable(String), "
        "path LowCardinality(String), model LowCardinality(Nullable(String)), "
        "status_code UInt16, is_error Bool, stream Bool, "
        "tokens_in Nullable(UInt32), tokens_out Nullable(UInt32), duration_ms Nullable(UInt32), "
        "user_agent Nullable(String)"
        ") ENGINE = ReplacingMergeTree ORDER BY (stack_id, created_at, id)"
    )
    lines = b"".join(_json({
        "id": str(r.id), "created_at": _clickhouse_time(r.end), "started_at": _clickhouse_time(r.start),
        "stack_id": r.stack_id, "api_key_id": r.api_key_id, "api_key_name": r.api_key_name,
        "path": r.path, "model": r.model, "status_code": r.status_code, "is_error": r.is_error,
        "stream": r.stream, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
        "duration_ms": r.duration_ms, "user_agent": r.user_agent,
    }) + b"\n" for r in records)
    base = url.rstrip("/") + "/"
    return [
        Delivery(base, headers, create.encode()),
        Delivery(base, headers, lines, params={"query": f"INSERT INTO {table} FORMAT JSONEachRow"}),
    ]


BUILDERS = {
    "datadog": build_datadog,
    "posthog": build_posthog,
    "clickhouse": build_clickhouse,
    "langfuse": build_langfuse,
    "langsmith": build_langsmith,
    "sentry": build_sentry,
    "grafana": build_grafana,
    "opentelemetry": build_opentelemetry,
    "newrelic": build_newrelic,
}


def build_deliveries(provider: str, config: dict, secrets: dict, stack_id: str, rows: list[dict]) -> list[Delivery]:
    builder = BUILDERS.get(provider)
    if builder is None:
        raise ConfigError(f"destino desconhecido: {provider}")
    return builder(config or {}, secrets or {}, [to_record(row, stack_id) for row in rows])
