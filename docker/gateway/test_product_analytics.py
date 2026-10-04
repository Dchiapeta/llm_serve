import asyncio
import json

import httpx
import pytest

import product_analytics
from product_analytics import StreamCompletion, capture_event, enabled, export_once
from reasoning_filter import filtered_reasoning_stream
from anthropic_compat import anthropic_sse_from_openai_stream

ACCOUNT = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
ENV = {"POSTHOG_GATEWAY_ENVIRONMENT": "production", "POSTHOG_PROJECT_TOKEN": "test-project-token"}


def row():
    return {
        "id": EVENT_ID, "event": "first_inference_completed", "distinct_id": USER,
        "event_timestamp": "2026-10-01T12:34:56.123+00:00",
        "properties": {"account_id": ACCOUNT, "model": "qwen3.5", "tokens_in": 8,
                       "$internal_or_test_user": True},
    }


def test_production_requires_explicit_environment_and_token():
    assert enabled(ENV)
    assert not enabled({"POSTHOG_PROJECT_TOKEN": "test"})
    assert not enabled({**ENV, "POSTHOG_GATEWAY_ENVIRONMENT": "preview"})
    assert not enabled({"POSTHOG_GATEWAY_ENVIRONMENT": "production"})


def test_privacy_allowlist_at_network_boundary():
    value = row()
    value["properties"].update({
        "prompt": "private prompt", "output": "private response", "email": "user@example.test",
        "headers": {"Authorization": "private key"}, "api_key": "private key",
        "$set": {"email": "user@example.test"}, "model": "user@example.test",
        "path": "https://example.test/?token=private", "duration_ms": float("nan"),
    })
    event = capture_event(value)
    serialized = json.dumps(event)
    assert "private" not in serialized and "user@example" not in serialized
    assert event["distinct_id"] == USER
    assert event["properties"]["model"] == "unknown"
    assert event["properties"]["$set"] == {"$internal_or_test_user": True}
    assert "duration_ms" not in event["properties"]
    assert capture_event(value) == event


@pytest.mark.parametrize("field,value", [("distinct_id", "email@example.test"), ("id", "invalid"),
    ("event", "$ai_generation"), ("event_timestamp", "2026-10-01T12:34:56")])
def test_invalid_identity_event_or_timestamp_rejected(field, value):
    source = row()
    source[field] = value
    with pytest.raises(ValueError):
        capture_event(source)


@pytest.mark.parametrize("chunks,expected", [
    ([b'data: [DO', b'NE]\n\n'], 200),
    ([b'event: response.completed\ndata: {"type":"response.completed"}\n\n'], 200),
    ([b'data: {"type":"message_stop"}\n\n'], 200),
    ([b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'], 502),
    ([b'data: {"error":{"message":"private"}}\n\ndata: [DONE]\n\n'], 502),
    ([b'event: response.incomplete\ndata: [DONE]\n\n'], 502),
    ([b'data: {"choices":[{"delta":{"content":"error"}}]}\n\ndata: [DONE]\n\n'], 200),
])
def test_stream_requires_terminal_and_no_error(chunks, expected):
    completion = StreamCompletion()
    for chunk in chunks:
        completion.feed(chunk)
    assert completion.status(200, exhausted=True) == expected


def test_terminal_before_abort_is_not_success():
    completion = StreamCompletion()
    completion.feed(b"data: [DONE]\n\n")
    assert completion.status(499, exhausted=False) == 499
    assert completion.status(200, exhausted=False) == 502


@pytest.mark.parametrize("kind", ["response.completed", "message_stop"])
def test_terminal_header_and_data_wait_for_sse_dispatch_delimiter(kind):
    completion = StreamCompletion()
    completion.feed(f"event: {kind}\n".encode())
    assert not completion.terminal
    completion.feed(b"data: " + json.dumps({"type": kind}).encode() + b"\n")
    assert not completion.terminal
    completion.feed(b"\n")
    assert completion.terminal
    assert completion.status(200, exhausted=True) == 200


@pytest.mark.parametrize("tail", [b"event: response.completed\n", b"data: [DONE]\n",
    b'data: {"type":"response.completed"}\n', b"data: [DONE]"])
def test_eof_before_sse_dispatch_is_truncated(tail):
    completion = StreamCompletion()
    completion.feed(tail)
    assert completion.status(200, exhausted=True) == 502


def test_malformed_sse_pending_line_has_a_bound_and_cannot_prove_success():
    completion = StreamCompletion()
    for _ in range(10):
        completion.feed(b"x" * (1024 * 1024))
        assert len(completion.pending) <= completion.MAX_LINE
    completion.feed(b"\ndata: [DONE]\n\n")
    assert completion.status(200, exhausted=True) == 502


class FakeUpstream:
    status_code = 200

    def __init__(self, terminal=True):
        self.closed = False
        self.terminal = terminal

    async def aiter_bytes(self):
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        if self.terminal:
            yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


class FragmentedUpstream(FakeUpstream):
    def __init__(self):
        super().__init__()
        self.read_count = 0

    async def aiter_bytes(self):
        for chunk in [b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
            b'event: message_stop\n', b'data: {"type":"message_stop"}\n', b'\n']:
            self.read_count += 1
            yield chunk
        await asyncio.sleep(10)  # término válido não espera EOF/timeout


@pytest.mark.parametrize("protocol", ["filtered", "anthropic"])
def test_fragmented_terminal_frame_is_consumed_completely(protocol):
    upstream = FragmentedUpstream()
    captured = []

    async def go():
        if protocol == "filtered":
            stream = filtered_reasoning_stream(upstream, ttft_s=1, idle_s=1,
                thinking_esperado=False, on_close=lambda status, usage: captured.append(status))
        else:
            stream = anthropic_sse_from_openai_stream(upstream, "qwen3.5", thinking_esperado=False,
                on_done=lambda usage, status: captured.append(status))
        async for _ in stream:
            pass

    asyncio.run(asyncio.wait_for(go(), timeout=1))
    assert upstream.read_count == 4 and upstream.closed
    assert captured == [200]


@pytest.mark.parametrize("protocol", ["filtered", "anthropic"])
def test_task_cancellation_during_upstream_read_is_aborted(protocol):
    captured = []
    ready = asyncio.Event()

    class WaitingUpstream(FakeUpstream):
        async def aiter_bytes(self):
            ready.set()
            await asyncio.sleep(10)
            yield b"data: [DONE]\n\n"

    upstream = WaitingUpstream()

    async def go():
        async def consume():
            if protocol == "filtered":
                stream = filtered_reasoning_stream(upstream, ttft_s=0, idle_s=0,
                    thinking_esperado=False, on_close=lambda status, usage: captured.append(status))
            else:
                stream = anthropic_sse_from_openai_stream(upstream, "qwen3.5", thinking_esperado=False,
                    on_done=lambda usage, status: captured.append(status))
            async for _ in stream:
                pass

        task = asyncio.create_task(consume())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert upstream.closed and captured == [499]


@pytest.mark.parametrize("protocol", ["filtered", "anthropic"])
def test_client_generator_close_is_logged_499(protocol):
    captured = []
    upstream = FakeUpstream()

    async def go():
        if protocol == "filtered":
            stream = filtered_reasoning_stream(upstream, ttft_s=1, idle_s=1,
                thinking_esperado=False, on_close=lambda status, usage: captured.append(status))
        else:
            stream = anthropic_sse_from_openai_stream(upstream, "qwen3.5", thinking_esperado=False,
                on_done=lambda usage, status: captured.append(status))
            await anext(stream)  # message_start precede a leitura do upstream
        await anext(stream)
        await stream.aclose()

    asyncio.run(go())
    assert upstream.closed
    assert captured == [499]


def test_anthropic_close_immediately_after_message_start_is_aborted():
    captured = []
    upstream = FakeUpstream()

    async def go():
        stream = anthropic_sse_from_openai_stream(upstream, "qwen3.5", thinking_esperado=False,
            on_done=lambda usage, status: captured.append(status))
        assert b"message_start" in await anext(stream)
        await stream.aclose()

    asyncio.run(go())
    assert upstream.closed and captured == [499]


@pytest.mark.parametrize("protocol", ["filtered", "anthropic"])
def test_clean_eof_without_terminal_is_not_success(protocol):
    captured = []
    upstream = FakeUpstream(terminal=False)

    async def go():
        if protocol == "filtered":
            stream = filtered_reasoning_stream(upstream, ttft_s=1, idle_s=1,
                thinking_esperado=False, on_close=lambda status, usage: captured.append(status))
        else:
            stream = anthropic_sse_from_openai_stream(upstream, "qwen3.5", thinking_esperado=False,
                on_done=lambda usage, status: captured.append(status))
        async for _ in stream:
            pass

    asyncio.run(go())
    assert captured == [502]


class FakeSupa:
    def __init__(self):
        self.prepared = 0
        self.acked = []

    async def prepare_gateway_analytics_exports(self):
        self.prepared += 1

    async def claim_gateway_analytics_exports(self, limit, batch_size):
        return [row()]

    async def ack_gateway_analytics_exports(self, ids):
        self.acked.extend(ids)

    async def discard_gateway_analytics_exports(self, ids):
        self.discarded = getattr(self, "discarded", []) + ids


def test_failed_delivery_retries_identical_envelope_and_only_acks_success():
    supa = FakeSupa()
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(503 if len(sent) == 1 else 200, json={"status": 1})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await export_once(supa, client, ENV)
            assert supa.acked == []
            assert await export_once(supa, client, ENV) == 1
            assert await export_once(supa, client, {}) == 0

    asyncio.run(go())
    assert sent[0] == sent[1]
    assert supa.prepared == 2
    assert supa.acked == [EVENT_ID]


BAD_ID = "44444444-4444-4444-8444-444444444444"


@pytest.mark.parametrize("breakage", [
    {"event_timestamp": "2026-10-01T12:34:56"}, {"event_timestamp": None},
    {"properties": "not-an-object"}, {"properties": {}},
])
def test_invalid_row_is_discarded_without_blocking_the_rest_of_the_batch(breakage):
    class Mixed(FakeSupa):
        async def claim_gateway_analytics_exports(self, limit, batch_size):
            return [{**row(), "id": BAD_ID, **breakage}, row()]

    supa = Mixed()
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": 1})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await export_once(supa, client, ENV) == 2

    asyncio.run(go())
    assert supa.discarded == [BAD_ID]
    assert [event["uuid"] for event in sent[0]["batch"]] == [EVENT_ID]
    assert supa.acked == [EVENT_ID]


def test_batch_with_only_invalid_rows_discards_without_network():
    class AllBad(FakeSupa):
        async def claim_gateway_analytics_exports(self, limit, batch_size):
            return [{**row(), "event_timestamp": None}]

    supa = AllBad()

    def handler(request):
        raise AssertionError("nothing valid to send")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await export_once(supa, client, ENV) == 1

    asyncio.run(go())
    assert supa.discarded == [EVENT_ID] and supa.acked == []


def test_export_loop_survives_unexpected_errors(monkeypatch):
    calls = []

    async def flaky(supa, client):
        calls.append(1)
        if len(calls) == 1:
            raise TypeError("unexpected")
        raise asyncio.CancelledError

    async def no_sleep(_):
        pass

    monkeypatch.setenv("POSTHOG_GATEWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "test-token")
    monkeypatch.setattr(product_analytics, "export_once", flaky)
    monkeypatch.setattr(product_analytics.asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(product_analytics.export_loop(object()))
    assert len(calls) == 2
