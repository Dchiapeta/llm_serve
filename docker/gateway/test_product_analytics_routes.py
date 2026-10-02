"""Seams reais do gateway, usando somente transportes locais em memória."""
import asyncio
import json

import httpx
import pytest

from test_openrouter_routes import POD, TEXT_SLUG, reserva, rota  # fixtures HTTP existentes
import main
from trigger_ctx import trigger_ctx


class FragmentedBody(httpx.AsyncByteStream):
    def __init__(self, kind):
        self.kind = kind
        self.read_count = 0

    async def __aiter__(self):
        payload = {"type": self.kind, "response": {"usage": {
            "input_tokens": 11, "output_tokens": 7}}}
        for chunk in [f"event: {self.kind}\n".encode(),
            b"data: " + json.dumps(payload).encode() + b"\n", b"\n"]:
            self.read_count += 1
            yield chunk
        await asyncio.sleep(10)


@pytest.mark.parametrize("provider", ["machine", "openrouter"])
def test_raw_stream_preserves_fragmented_terminal_payload_and_usage(reserva, provider):
    upstream = FragmentedBody("response.completed")
    response = lambda request: httpx.Response(200, stream=upstream,
        headers={"content-type": "text/event-stream"})
    if provider == "machine":
        reserva["machine"] = POD
        reserva["pod_resposta"] = response
    else:
        reserva["resposta"] = response
    result = reserva["client"].post("/v1/responses", json={
        "model": TEXT_SLUG, "input": "hello", "stream": True},
        headers={"Authorization": "Bearer test-key"})
    assert result.status_code == 200
    assert upstream.read_count == 3
    assert b'"input_tokens": 11' in result.content
    assert result.content.endswith(b"\n\n")
    assert reserva["logged"][0]["status_code"] == 200
    # Responses usage permanece disponível após o header SSE fragmentado.
    assert reserva["logged"][0]["usage"]["prompt_tokens"] == 11


@pytest.mark.parametrize("provider", ["filtered_machine", "openrouter"])
@pytest.mark.parametrize("valid", [False, True])
def test_nonstream_malformed_body_changes_only_ledger_status(reserva, provider, valid):
    visible = "<think>reasoning</think>hello" if provider == "filtered_machine" else "hello"
    raw = json.dumps({"id": "test", "model": "pro-base", "choices": [{
        "message": {"role": "assistant", "content": visible}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode() if valid else b'{"choices":'
    response = lambda request: httpx.Response(200, content=raw,
        headers={"content-type": "application/json"})
    if provider == "filtered_machine":
        reserva["machine"] = POD
        reserva["plan"] = "Pro"
        reserva["pod_resposta"] = response
    else:
        reserva["resposta"] = response
    result = reserva["client"].post("/v1/chat/completions", json={
        "model": TEXT_SLUG, "messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": "Bearer test-key"})
    assert result.status_code == 200
    if not valid:
        assert result.content == raw
    else:
        assert result.json()["choices"][0]["message"]["content"] == "hello"
    assert reserva["logged"][0]["status_code"] == (200 if valid else 502)


@pytest.mark.parametrize("failure,retry", [
    ("analytics_column_missing", True), ("timeout_after_commit", False),
    ("other_missing_column", False), ("server_error", False),
])
def test_ledger_fallback_only_retries_uncommitted_missing_analytics_column(monkeypatch, failure, retry):
    calls = []
    committed = []
    request = httpx.Request("POST", "https://database.test/rest/v1/gateway_requests")

    class Ledger:
        async def insert_gateway_request(self, row):
            calls.append(row)
            if len(calls) == 1:
                if failure == "timeout_after_commit":
                    committed.append(row)
                    raise httpx.ReadTimeout("response lost after commit", request=request)
                field = "analytics_outcome" if failure == "analytics_column_missing" else "other_column"
                response = httpx.Response(500 if failure == "server_error" else 400,
                    json={"code": "PGRST204", "message": f"Could not find the '{field}' column"},
                    request=request)
                raise httpx.HTTPStatusError("schema error", request=request, response=response)
            committed.append(row)

    monkeypatch.setattr(main, "supa", Ledger(), raising=False)
    row = {"account_id": "account", "analytics_origin": "customer", "analytics_outcome": "completed"}
    asyncio.run(main._write_gateway_request(row))
    assert len(calls) == (2 if retry else 1)
    assert len(committed) <= 1
    if retry:
        assert committed == [{"account_id": "account"}]
    if failure == "timeout_after_commit":
        assert committed == [row]


@pytest.mark.parametrize("purpose,status,outcome", [
    ("customer", 200, "completed"), ("playground", 499, "aborted"),
    ("customer", 502, "error"),
])
def test_request_purpose_and_outcome_are_materialized_before_background_task(monkeypatch, purpose, status, outcome):
    pending = []
    rows = []

    async def record(row):
        rows.append(row)

    monkeypatch.setenv("POSTHOG_GATEWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "test-token")
    monkeypatch.setattr(main, "_write_gateway_request", record)
    monkeypatch.setattr(main, "spawn_tracked", pending.append)
    token = trigger_ctx.set({"purpose": purpose})
    try:
        main.log_gateway_request(account_id="account", stack_id="stack", api_key_id="key",
            machine_id="machine", path="responses", model="served-model", status_code=status,
            stream=True, started=main.time.monotonic())
        trigger_ctx.set({"purpose": "changed_after_request"})

        async def run():
            for coroutine in pending:
                await coroutine

        asyncio.run(run())
    finally:
        trigger_ctx.reset(token)
    assert rows[0]["analytics_origin"] == purpose
    assert rows[0]["analytics_outcome"] == outcome
