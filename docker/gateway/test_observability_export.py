import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import observability_export as export
import observability_formats as formats

STACK = "11111111-1111-4111-8111-111111111111"
KEY = "22222222-2222-4222-8222-222222222222"
ROW_OK = "33333333-3333-4333-8333-333333333333"
ROW_ERR = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def row(row_id=ROW_OK, status=200, path="chat/completions", **extra):
    return {
        "id": row_id, "created_at": "2026-10-21T11:58:00.500000+00:00", "api_key_id": KEY,
        "api_key_name": "produção", "path": path, "model": "qwen/qwen3.5-9b", "status_code": status,
        "stream": True, "tokens_in": 120, "tokens_out": 45, "duration_ms": 1500,
        "user_agent": "openai-python/1.0", **extra,
    }


def build(provider, config, secrets, rows=None):
    return formats.build_deliveries(provider, config, secrets, STACK, rows or [row(), row(ROW_ERR, 502)])


async def public_resolver(host, port):
    return ["93.184.216.34"]


# ---------- recorte e registro ----------

def test_select_rows_follows_toggles_and_skips_models():
    rows = [row(), row(ROW_ERR, 502), row("55555555-5555-4555-8555-555555555555", path="models")]
    assert [r["id"] for r in formats.select_rows(rows, {"requests": True, "errors": True})] == [ROW_OK, ROW_ERR]
    assert [r["id"] for r in formats.select_rows(rows, {"requests": True, "errors": False})] == [ROW_OK]
    assert [r["id"] for r in formats.select_rows(rows, {"requests": False, "errors": True})] == [ROW_ERR]
    assert formats.select_rows(rows, {"content": True}) == []


def test_record_derives_start_from_duration_and_499_message():
    r = formats.to_record(row(status=499), STACK)
    assert r.end - r.start == timedelta(milliseconds=1500)
    assert r.error_message == "client closed request (499)"
    assert r.name == "chat qwen/qwen3.5-9b"
    assert formats.to_record(row(path="embeddings"), STACK).operation == "embeddings"
    assert formats.to_record(row(path="images/generations"), STACK).operation == "generate_content"


# ---------- formatos ----------

def test_otlp_grafana_is_binary_protobuf_with_basic_auth():
    [d] = build("grafana", {"endpoint": "https://otlp-gateway-prod-us-east-0.grafana.net/otlp/", "instanceId": "123"},
                {"token": "glc_secret"})
    assert d.url == "https://otlp-gateway-prod-us-east-0.grafana.net/otlp/v1/traces"
    assert d.headers["Content-Type"] == "application/x-protobuf"
    assert d.headers["Authorization"] == "Basic " + base64.b64encode(b"123:glc_secret").decode()
    request = ExportTraceServiceRequest.FromString(d.content)
    spans = request.resource_spans[0].scope_spans[0].spans
    assert [s.name for s in spans] == ["chat qwen/qwen3.5-9b"] * 2
    assert spans[0].end_time_unix_nano - spans[0].start_time_unix_nano == 1_500_000_000
    attrs = {kv.key: kv.value for kv in spans[0].attributes}
    assert attrs["gen_ai.usage.input_tokens"].int_value == 120
    assert attrs["stac.api_key.name"].string_value == "produção"
    assert spans[0].status.code == 0 and spans[1].status.code == 2
    assert spans[1].status.message == "HTTP 502"


def test_otlp_generic_keeps_full_path_and_parses_headers():
    [d] = build("opentelemetry", {"endpoint": "https://collector.example.com:4318/v1/traces"},
                {"headers": "Authorization=Bearer abc, x-tenant=acme"})
    assert d.url == "https://collector.example.com:4318/v1/traces"
    assert d.headers["Authorization"] == "Bearer abc" and d.headers["x-tenant"] == "acme"


def test_newrelic_region_and_license_header():
    [d] = build("newrelic", {"region": "EU"}, {"licenseKey": "abcNRAL"})
    assert d.url == "https://otlp.eu01.nr-data.net:4318/v1/traces"
    assert d.headers["api-key"] == "abcNRAL"
    with pytest.raises(formats.ConfigError):
        build("newrelic", {"region": "BR"}, {"licenseKey": "x"})


def test_datadog_llm_obs_spans():
    [d] = build("datadog", {"site": "datadoghq.eu"}, {"apiKey": "dd_secret"})
    assert d.url == "https://api.datadoghq.eu/api/intake/llm-obs/v1/trace/spans"
    assert d.headers["DD-API-KEY"] == "dd_secret"
    body = json.loads(d.content)
    spans = body["data"]["attributes"]["spans"]
    assert body["data"]["type"] == "span" and body["data"]["attributes"]["ml_app"] == "stac"
    assert spans[0]["parent_id"] == "undefined"
    assert spans[0]["duration"] == 1_500_000_000
    assert spans[0]["metrics"] == {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165}
    assert spans[1]["status"] == "error" and spans[1]["meta"]["error"]["message"] == "HTTP 502"


def test_posthog_ai_generation_without_person_profile():
    [d] = build("posthog", {"host": "https://eu.i.posthog.com"}, {"projectApiKey": "phc_x"})
    assert d.url == "https://eu.i.posthog.com/batch/"
    body = json.loads(d.content)
    assert body["api_key"] == "phc_x"
    event = body["batch"][0]
    assert event["event"] == "$ai_generation" and event["uuid"] == ROW_OK
    assert event["properties"]["$ai_latency"] == 1.5
    assert event["properties"]["$process_person_profile"] is False
    assert body["batch"][1]["properties"]["$ai_is_error"] is True


def test_langfuse_trace_and_generation_with_stable_ids():
    [d] = build("langfuse", {"publicKey": "pk-lf-1", "host": "https://cloud.langfuse.com"}, {"secretKey": "sk-lf-1"})
    assert d.url == "https://cloud.langfuse.com/api/public/ingestion"
    assert d.headers["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-1").decode()
    batch = json.loads(d.content)["batch"]
    assert [e["type"] for e in batch] == ["trace-create", "generation-create"] * 2
    assert batch[1]["body"]["traceId"] == ROW_OK
    assert batch[1]["body"]["usageDetails"] == {"input": 120, "output": 45}
    assert batch[3]["body"]["level"] == "ERROR"
    [again] = build("langfuse", {"publicKey": "pk-lf-1"}, {"secretKey": "sk-lf-1"})
    assert [e["id"] for e in json.loads(again.content)["batch"]] == [e["id"] for e in batch]


def test_langsmith_runs_batch():
    [d] = build("langsmith", {"project": "prod"}, {"apiKey": "lsv2_pt_x"})
    assert d.url == "https://api.smith.langchain.com/runs/batch"
    run = json.loads(d.content)["post"][0]
    assert run["run_type"] == "llm" and run["session_name"] == "prod"
    assert run["dotted_order"] == "20261021T115759000000Z" + ROW_OK
    assert run["outputs"]["usage_metadata"]["total_tokens"] == 165


def test_sentry_one_transaction_envelope_per_row():
    dsn = "https://pubkey@o1.ingest.us.sentry.io/4507"
    deliveries = build("sentry", {}, {"dsn": dsn})
    assert len(deliveries) == 2
    d = deliveries[1]
    assert d.url == "https://o1.ingest.us.sentry.io/api/4507/envelope/"
    assert "sentry_key=pubkey" in d.headers["X-Sentry-Auth"]
    header, item, payload = d.content.decode().strip().split("\n")
    assert json.loads(item) == {"type": "transaction"}
    transaction = json.loads(payload)
    assert transaction["contexts"]["trace"]["status"] == "internal_error"
    assert transaction["contexts"]["trace"]["op"] == "gen_ai.chat"
    with pytest.raises(formats.ConfigError):
        formats.parse_sentry_dsn("https://o1.ingest.sentry.io/4507")


def test_clickhouse_create_and_insert_with_safe_identifiers():
    create, insert = build("clickhouse", {"url": "https://x.clickhouse.cloud:8443", "username": "default",
                                          "database": "analytics"}, {"password": "pw"})
    assert create.content.startswith(b"CREATE TABLE IF NOT EXISTS `analytics`.`stac_requests`")
    assert insert.params == {"query": "INSERT INTO `analytics`.`stac_requests` FORMAT JSONEachRow"}
    assert insert.headers == {"X-ClickHouse-User": "default", "X-ClickHouse-Key": "pw"}
    first = json.loads(insert.content.splitlines()[0])
    assert first["created_at"] == "2026-10-21 11:58:00.500"
    assert first["started_at"] == "2026-10-21 11:57:59.000"
    with pytest.raises(formats.ConfigError):
        build("clickhouse", {"url": "https://x", "username": "u", "table": "t`; DROP"}, {"password": "pw"})


def test_missing_secret_is_config_error():
    with pytest.raises(formats.ConfigError):
        build("datadog", {"site": "datadoghq.com"}, {})


# ---------- segurança da entrega ----------

@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.10", "::1", "fd00::1"])
def test_private_destinations_are_refused(address):
    async def resolver(host, port):
        return [address]
    with pytest.raises(export.DeliveryError):
        asyncio.run(export.ensure_public_host("https://evil.example.com/x", resolver))


def test_public_destination_passes_and_http_is_refused():
    asyncio.run(export.ensure_public_host("https://api.datadoghq.com/x", public_resolver))
    with pytest.raises(export.DeliveryError):
        asyncio.run(export.ensure_public_host("http://api.datadoghq.com/x", public_resolver))


def test_error_message_hides_secrets_and_5xx_body():
    response = httpx.Response(403, text="invalid key dd_secret_123\nfor org")
    assert export.describe_response(response, {"apiKey": "dd_secret_123"}) == "HTTP 403: invalid key *** for org"
    assert export.describe_response(httpx.Response(502, text="<html>stack trace</html>"), {}) == "HTTP 502"
    datadog = httpx.Response(403, json={"errors": ["Forbidden: API key dd_secret_123 is invalid"]})
    assert export.describe_response(datadog, {"apiKey": "dd_secret_123"}) == "HTTP 403: Forbidden: API key *** is invalid"
    posthog = httpx.Response(401, json={"type": "authentication_error", "detail": "Invalid token"})
    assert export.describe_response(posthog, {}) == "HTTP 401: Invalid token"


def test_backoff_grows_and_caps():
    assert [export.backoff_seconds(n) for n in (0, 1, 2, 3)] == [0, 30, 60, 120]
    assert export.backoff_seconds(30) == 3600
    failing = {"consecutive_failures": 2, "last_error_at": (NOW - timedelta(seconds=30)).isoformat()}
    assert export.in_backoff(failing, NOW)
    assert not export.in_backoff({**failing, "last_error_at": (NOW - timedelta(minutes=5)).isoformat()}, NOW)


# ---------- o loop ----------

class FakeSupa:
    def __init__(self, integration, pages, updated_at_matches=True):
        self.integration = integration
        self.pages = list(pages)
        self.updates = []
        self.rpc_calls = []
        self.updated_at_matches = updated_at_matches

    async def list_exportable_stack_integrations(self, now_iso, limit):
        return [self.integration]

    async def lease_stack_integration(self, integration_id, updated_at, now_iso, until_iso):
        return {**self.integration, "lease_until": until_iso}

    async def stack_integration_pending_requests(self, stack_id, after_at, after_id, until_iso, limit):
        self.rpc_calls.append((after_at, after_id, until_iso, limit))
        return self.pages.pop(0) if self.pages else []

    async def update_stack_integration(self, integration_id, updated_at, patch):
        self.updates.append(patch)
        return self.updated_at_matches


def integration(box_key, provider="posthog", **extra):
    secrets = Fernet(box_key).encrypt(json.dumps({"projectApiKey": "phc_secret"}).encode()).decode()
    return {
        "id": "66666666-6666-4666-8666-666666666666", "stack_id": STACK, "provider": provider,
        "enabled": True, "config": {"host": "https://us.i.posthog.com"}, "secrets_encrypted": secrets,
        "events": {"requests": True, "errors": True, "content": False},
        "cursor_at": "2026-10-21T11:00:00+00:00", "cursor_id": None, "consecutive_failures": 0,
        "last_error_at": None, "updated_at": "2026-10-21T10:00:00+00:00", **extra,
    }


def run_once(supa, handler, key):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await export.export_once(supa, client, export.SecretBox(key.decode()), NOW, public_resolver)
    return asyncio.run(go())


def test_export_sends_then_advances_cursor_and_releases_lease():
    key = Fernet.generate_key()
    supa = FakeSupa(integration(key), [[row(), row(ROW_ERR, 502)]])
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": 1})

    assert run_once(supa, handler, key) == 2
    assert seen[0]["api_key"] == "phc_secret"
    advance, release = supa.updates
    assert advance["cursor_id"] == ROW_ERR and advance["consecutive_failures"] == 0
    assert "last_success_at" in advance
    assert release == {"lease_until": None}
    # corte de 30s: linhas recém-gravadas esperam o próximo tick
    assert supa.rpc_calls[0][2] == (NOW - timedelta(seconds=30)).isoformat()


def test_export_failure_keeps_cursor_and_records_scrubbed_error():
    key = Fernet.generate_key()
    supa = FakeSupa(integration(key), [[row()]])

    def handler(request):
        return httpx.Response(401, text="bad token phc_secret")

    assert run_once(supa, handler, key) == 0
    [failure] = supa.updates
    assert "cursor_at" not in failure
    assert failure["last_error"] == "HTTP 401: bad token ***"
    assert failure["consecutive_failures"] == 1 and failure["lease_until"] is None


def test_rows_filtered_out_still_advance_cursor_without_success_stamp():
    key = Fernet.generate_key()
    supa = FakeSupa(integration(key, events={"requests": False, "errors": True}), [[row()]])

    def handler(request):
        raise AssertionError("nada a enviar")

    assert run_once(supa, handler, key) == 0
    advance = supa.updates[0]
    assert advance["cursor_id"] == ROW_OK and "last_success_at" not in advance


def test_edit_during_export_stops_without_touching_the_row_again():
    key = Fernet.generate_key()
    supa = FakeSupa(integration(key), [[row()]], updated_at_matches=False)
    run_once(supa, lambda request: httpx.Response(200), key)
    assert len(supa.updates) == 1  # só a tentativa de avanço; sem soltar lease


def test_unreadable_secret_is_reported_once():
    key = Fernet.generate_key()
    other = Fernet.generate_key()
    supa = FakeSupa(integration(other), [[row()]])
    run_once(supa, lambda request: httpx.Response(200), key)
    assert supa.updates[0]["last_error"] == "credencial ilegível: salve a integração de novo"
    assert supa.rpc_calls == []


def test_disabled_without_key():
    assert not export.enabled({})
    assert export.enabled({"INTEGRATIONS_ENCRYPTION_KEY": "x"})
    assert not export.enabled({"INTEGRATIONS_ENCRYPTION_KEY": "x", "OBSERVABILITY_EXPORT": "disabled"})
