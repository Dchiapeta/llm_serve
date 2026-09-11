import asyncio
import json
import logging
import os

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import main
from generation_trace import request_trace_id
from rag_policy import is_isolated_greeting

ENTRY = {"account_id": "a1", "api_key_id": "k1", "stack_id": "s1",
         "stacks": [{"id": "s1", "plan": "Go", "system_prompt": "Seja breve."}]}


class FakeSupa:
    """Só o método de busca que a política de RAG chama."""

    def __init__(self, search):
        self.match_knowledge_chunks = search


def _install_supa(monkeypatch, search):
    # raising=False: `supa` é só uma ANOTAÇÃO em main.py (main.py:567) — o
    # atributo passa a existir no lifespan, que estes testes não rodam. Mesmo
    # padrão de test_image_routes.py.
    monkeypatch.setattr(main, "supa", FakeSupa(search), raising=False)


@pytest.mark.parametrize("text", [" OPA!!! ", "Olá!", "bom DIA", "boa noite."])
def test_isolated_greetings(text):
    assert is_isolated_greeting(text)


@pytest.mark.parametrize("text", ["Certificações?", "opa, quais são minhas certificações?", "bom dia amanhã?", "E elas?", "Crie um poema"])
def test_questions_do_not_skip_search(text):
    assert not is_isolated_greeting(text)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_greeting_never_calls_embedding_or_database(monkeypatch, caplog, protocol):
    async def forbidden(*args, **kwargs):
        pytest.fail("isolated greeting must not search")
    monkeypatch.setattr(main, "embed_query", forbidden)
    _install_supa(monkeypatch, forbidden)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    token = request_trace_id.set("trace-test")
    try:
        if protocol == "chat":
            result = asyncio.run(main.build_stack_system_message([{"role": "user", "content": "opa"}], ENTRY))
            assert result["content"] == "Seja breve."
        else:
            assert asyncio.run(main.build_stack_instructions(ENTRY, "opa")) == "Seja breve."
    finally:
        request_trace_id.reset(token)
    event = json.loads(caplog.records[-1].message.split("generation_policy ")[1])
    assert event["request_id"] == "trace-test"
    assert event["reason"] == "isolated_greeting"
    assert event["inserted_chunks"] == 0


def test_relevant_question_preserves_stack_scope_and_counts(monkeypatch, caplog):
    async def embedding(text):
        assert text == "Quais são minhas certificações?"
        return [1.0]
    async def search(account, stack, embedding, top_k):
        assert (account, stack, embedding, top_k) == ("a1", "s1", [1.0], main.RAG_TOP_K)
        return ["PRIVATE DOCUMENT"]
    monkeypatch.setattr(main, "embed_query", embedding)
    _install_supa(monkeypatch, search)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = asyncio.run(main.build_stack_system_message([
        {"role": "user", "content": "Quais são minhas certificações?"}], ENTRY))
    assert "PRIVATE DOCUMENT" in result["content"]
    assert "PRIVATE DOCUMENT" not in caplog.text
    assert '"inserted_chunks": 1' in caplog.text


def test_client_system_prevents_gateway_rag(monkeypatch, caplog):
    async def forbidden(*args, **kwargs):
        pytest.fail("client system must bypass gateway RAG")
    async def estimate(*args, **kwargs):
        return 100, None
    monkeypatch.setattr(main, "embed_query", forbidden)
    monkeypatch.setattr(main, "resolve_est_tokens", estimate)
    monkeypatch.setattr(main, "apply_context_budget", lambda *args, **kwargs: None)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "system", "content": "CV FROM CLIENT"}, {"role": "user", "content": "opa"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        ENTRY, False, {"served_model_name": "go-base"}, "s1"))
    assert result["messages"] == messages
    assert '"reason": "client_system"' in caplog.text
    assert "CV FROM CLIENT" not in caplog.text
