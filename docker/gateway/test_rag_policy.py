import asyncio
import json
import logging
import os

import pytest

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import main
from anthropic_compat import anthropic_to_openai_request
from generation_trace import request_trace_id
from rag_policy import RagPolicy, is_isolated_greeting, resolve_rag_policy

# Chave NÃO MIGRADA de propósito: sem a coluna enable_knowledge_base, é o
# fixture do caminho legado (migration 0065 ainda não aplicada, ou chave
# criada antes dela). Os testes da coluna montam o entry com _entry().
ENTRY = {"account_id": "a1", "api_key_id": "k1", "stack_id": "s1",
         "stacks": [{"id": "s1", "plan": "Go", "system_prompt": "Seja breve."}]}

MACHINE = {"served_model_name": "go-base"}


def _entry(enable_knowledge_base):
    """ENTRY com a coluna da 0065 preenchida — inclusive com lixo, pro teste
    de degradação. Cópia rasa porque nenhum teste escreve em `stacks`."""
    return {**ENTRY, "enable_knowledge_base": enable_knowledge_base}


class FakeSupa:
    """Só o método de busca que a política de RAG chama."""

    def __init__(self, search):
        self.match_knowledge_chunks = search


def _install_supa(monkeypatch, search):
    # raising=False: `supa` é só uma ANOTAÇÃO em main.py (main.py:567) — o
    # atributo passa a existir no lifespan, que estes testes não rodam. Mesmo
    # padrão de test_image_routes.py.
    monkeypatch.setattr(main, "supa", FakeSupa(search), raising=False)


def _stub_budget(monkeypatch):
    """Neutraliza o clamp de janela: ele faz I/O (tokenizer do pod) e não tem
    nada a ver com a decisão de RAG, que é o que estes testes medem."""
    async def estimate(*args, **kwargs):
        return 100, None
    monkeypatch.setattr(main, "resolve_est_tokens", estimate)
    monkeypatch.setattr(main, "apply_context_budget", lambda *args, **kwargs: None)


def _forbid_search(monkeypatch, message):
    async def forbidden(*args, **kwargs):
        pytest.fail(message)
    monkeypatch.setattr(main, "embed_query", forbidden)
    _install_supa(monkeypatch, forbidden)


def _install_search(monkeypatch, chunks, expect_text=None):
    seen = {}
    async def embedding(text):
        seen["text"] = text
        if expect_text is not None:
            assert text == expect_text
        return [1.0]
    async def search(account, stack, embedding_vector, top_k):
        return list(chunks)
    monkeypatch.setattr(main, "embed_query", embedding)
    _install_supa(monkeypatch, search)
    return seen


def _last_event(caplog):
    return json.loads(caplog.records[-1].message.split("generation_policy ")[1])


def _rag_events(caplog):
    events = [json.loads(r.message.split("generation_policy ")[1])
              for r in caplog.records if "generation_policy " in r.message]
    return [e for e in events if e["kind"] == "rag"]


# --------------------------------------------------------------------------
# Porta da saudação
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [" OPA!!! ", "Olá!", "bom DIA", "boa noite."])
def test_isolated_greetings(text):
    assert is_isolated_greeting(text)


@pytest.mark.parametrize("text", ["Certificações?", "opa, quais são minhas certificações?", "bom dia amanhã?", "E elas?", "Crie um poema"])
def test_questions_do_not_skip_search(text):
    assert not is_isolated_greeting(text)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_greeting_never_calls_embedding_or_database(monkeypatch, caplog, protocol):
    _forbid_search(monkeypatch, "isolated greeting must not search")
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
    event = _last_event(caplog)
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


# --------------------------------------------------------------------------
# Política pura (migration 0065)
# --------------------------------------------------------------------------

def test_unconfigured_key_is_legacy_not_disabled():
    # A distinção é o contrato inteiro da coluna: NULL preserva o
    # comportamento antigo, não desliga a base.
    policy = resolve_rag_policy(ENTRY)
    assert policy == RagPolicy(None, "legacy")
    assert policy.enabled is not False


@pytest.mark.parametrize("value", [True, False])
def test_explicit_boolean_comes_from_the_key(value):
    assert resolve_rag_policy(_entry(value)) == RagPolicy(value, "key")


@pytest.mark.parametrize("value", ["true", 1, 0, [], {}])
def test_non_boolean_degrades_to_legacy(value):
    # Assimetria deliberada com o thinking, que levanta erro: o RAG é
    # best-effort e não pode derrubar a inferência por causa de uma coluna
    # com lixo.
    assert resolve_rag_policy(_entry(value)) == RagPolicy(None, "legacy")


def test_missing_entry_is_legacy():
    assert resolve_rag_policy({}) == RagPolicy(None, "legacy")


# --------------------------------------------------------------------------
# Chat completions: o merge com o system do cliente
# --------------------------------------------------------------------------

def test_legacy_client_system_prevents_gateway_rag(monkeypatch, caplog):
    """Guarda o caminho NULL/legado: chave não migrada + system do cliente
    continua pulando a busca, exatamente como antes da 0065. É o comportamento
    que as CLIs (Cursor, Codex, Claude Code) dependem e que a coluna nova NÃO
    pode mudar por baixo."""
    _forbid_search(monkeypatch, "client system must bypass gateway RAG")
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "system", "content": "CV FROM CLIENT"}, {"role": "user", "content": "opa"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        ENTRY, False, MACHINE, "s1"))
    assert result["messages"] == messages
    assert '"reason": "client_system"' in caplog.text
    assert '"source": "legacy"' in caplog.text
    assert "CV FROM CLIENT" not in caplog.text


def test_enabled_key_merges_context_after_client_system(monkeypatch, caplog):
    """O teste central da 0065: com a flag ligada, o contexto entra POR CIMA da
    instrução do cliente, sem virar uma segunda mensagem system e sem trazer
    junto o system_prompt da stack."""
    _install_search(monkeypatch, ["PRIVATE DOCUMENT"],
                    expect_text="Quais são minhas certificações?")
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "system", "content": "CV FROM CLIENT"},
                {"role": "user", "content": "Quais são minhas certificações?"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(True), False, MACHINE, "s1"))

    systems = [m for m in result["messages"] if m["role"] == "system"]
    # UMA system, no índice 0 — o chat template do Qwen rejeita o resto.
    assert len(systems) == 1
    assert result["messages"][0]["role"] == "system"
    content = systems[0]["content"]
    # Instrução do cliente primeiro (prefixo estável = prefix cache do pod),
    # contexto depois.
    assert content.startswith("CV FROM CLIENT")
    assert "PRIVATE DOCUMENT" in content
    # PRECEDÊNCIA PRESERVADA: o system prompt configurado continua fora — só o
    # bloco de RAG passou a entrar.
    assert "Seja breve." not in content

    event = _rag_events(caplog)[-1]
    assert (event["reason"], event["source"], event["client_prompt"]) == ("retrieved", "key", True)


def test_enabled_key_without_results_leaves_messages_untouched(monkeypatch, caplog):
    # Sem chunk não há o que concatenar: o join de um elemento devolve o texto
    # do cliente byte a byte, então a request sai idêntica à que entrou.
    _install_search(monkeypatch, [])
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "system", "content": "CV FROM CLIENT"},
                {"role": "user", "content": "Quais são minhas certificações?"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(True), False, MACHINE, "s1"))
    assert result["messages"] == messages
    assert _rag_events(caplog)[-1]["reason"] == "no_results"


def test_disabled_key_keeps_stack_prompt_without_searching(monkeypatch, caplog):
    """Desligar a base desliga SÓ a base: sem system do cliente, o
    `system_prompt` da stack continua sendo injetado."""
    _forbid_search(monkeypatch, "disabled key must not search")
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "user", "content": "Quais são minhas certificações?"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(False), False, MACHINE, "s1"))
    assert result["messages"][0] == {"role": "system", "content": "Seja breve."}
    event = _rag_events(caplog)[-1]
    assert (event["reason"], event["source"], event["client_prompt"]) == ("disabled", "key", False)


def test_disabled_key_with_client_system_never_searches(monkeypatch, caplog):
    _forbid_search(monkeypatch, "disabled key must not search")
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "system", "content": "CV FROM CLIENT"},
                {"role": "user", "content": "Quais são minhas certificações?"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(False), False, MACHINE, "s1"))
    assert result["messages"] == messages
    event = _rag_events(caplog)[-1]
    assert (event["reason"], event["source"], event["client_prompt"]) == ("disabled", "key", True)


def test_enabled_key_still_skips_isolated_greeting(monkeypatch, caplog):
    # A flag decide SE a chave consulta; a porta da saudação continua valendo
    # depois disso. Ligar a base não pode passar a embedar "opa".
    _forbid_search(monkeypatch, "isolated greeting must not search")
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    messages = [{"role": "system", "content": "CV FROM CLIENT"}, {"role": "user", "content": "opa"}]
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(True), False, MACHINE, "s1"))
    assert result["messages"] == messages
    event = _rag_events(caplog)[-1]
    assert (event["reason"], event["source"], event["client_prompt"]) == ("isolated_greeting", "key", True)


# --------------------------------------------------------------------------
# content em lista de partes tipadas (o bug do extrator)
# --------------------------------------------------------------------------

def test_multimodal_user_reaches_retrieval(monkeypatch, caplog):
    """content em lista de partes é protocolo OpenAI válido e caía em
    no_user_text: a busca era pulada em silêncio pra todo cliente multimodal."""
    seen = _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = asyncio.run(main.build_stack_system_message([{"role": "user", "content": [
        {"type": "text", "text": "Quais são minhas certificações?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}], ENTRY))
    assert seen["text"] == "Quais são minhas certificações?"
    assert "PRIVATE DOCUMENT" in result["content"]
    assert _rag_events(caplog)[-1]["reason"] == "retrieved"


def test_image_only_user_has_no_text(monkeypatch, caplog):
    # Imagem não vira pergunta: sem parte de texto a busca continua pulada, e
    # o motivo tem que ser esse mesmo.
    _forbid_search(monkeypatch, "image-only user has nothing to embed")
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = asyncio.run(main.build_stack_system_message([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}], ENTRY))
    assert result["content"] == "Seja breve."
    assert _rag_events(caplog)[-1]["reason"] == "no_user_text"


def test_anthropic_multi_block_user_reaches_retrieval(monkeypatch, caplog):
    """Ponta a ponta pelo /v1/messages: anthropic_compat só emite string
    quando há exatamente UMA parte de texto, então dois blocos já produziam o
    formato de lista que o extrator antigo descartava."""
    seen = _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    caplog.set_level(logging.INFO, logger="gateway.generation")
    body, _ = anthropic_to_openai_request({"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Quais são minhas certificações?"},
        {"type": "text", "text": "Liste todas."},
    ]}]})
    assert not isinstance(body["messages"][-1]["content"], str)
    result = asyncio.run(main.build_stack_system_message(body["messages"], ENTRY))
    # Separador EXPLÍCITO entre os blocos. Sem ele o texto vira
    # "...certificações?Liste todas." e uma palavra inexistente entra bem no
    # meio da query que vai virar embedding. A versão anterior deste teste
    # passava por acidente: o primeiro bloco terminava em espaço.
    assert seen["text"] == "Quais são minhas certificações?\nListe todas."
    assert "PRIVATE DOCUMENT" in result["content"]


# --------------------------------------------------------------------------
# Responses API (Codex): "instructions" no papel do system
# --------------------------------------------------------------------------

def _run_responses(entry, body, monkeypatch):
    _stub_budget(monkeypatch)
    return asyncio.run(main.validate_responses_body(body, entry, False, MACHINE, "s1"))


def test_legacy_client_instructions_prevent_gateway_rag(monkeypatch, caplog):
    _forbid_search(monkeypatch, "client instructions must bypass gateway RAG")
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = _run_responses(ENTRY, {"instructions": "CV FROM CLIENT", "input": [
        {"role": "user", "content": "Quais são minhas certificações?"}]}, monkeypatch)
    assert result["instructions"] == "CV FROM CLIENT"
    event = _rag_events(caplog)[-1]
    assert (event["reason"], event["source"], event["client_prompt"]) == ("client_instructions", "legacy", True)


def test_enabled_key_merges_context_after_client_instructions(monkeypatch, caplog):
    _install_search(monkeypatch, ["PRIVATE DOCUMENT"],
                    expect_text="Quais são minhas certificações?")
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = _run_responses(_entry(True), {"instructions": "CV FROM CLIENT", "input": [
        {"role": "user", "content": "Quais são minhas certificações?"}]}, monkeypatch)
    assert result["instructions"].startswith("CV FROM CLIENT")
    assert "PRIVATE DOCUMENT" in result["instructions"]
    assert "Seja breve." not in result["instructions"]
    assert _rag_events(caplog)[-1]["client_prompt"] is True


def test_disabled_key_with_client_instructions_never_searches(monkeypatch, caplog):
    _forbid_search(monkeypatch, "disabled key must not search")
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = _run_responses(_entry(False), {"instructions": "CV FROM CLIENT", "input": [
        {"role": "user", "content": "Quais são minhas certificações?"}]}, monkeypatch)
    assert result["instructions"] == "CV FROM CLIENT"
    assert _rag_events(caplog)[-1]["reason"] == "disabled"


def test_non_string_instructions_do_not_raise(monkeypatch, caplog):
    # O campo vem cru do cliente na Responses API: um objeto aqui tem que cair
    # no trace, não estourar TypeError dentro do join.
    _forbid_search(monkeypatch, "non-string instructions must not search")
    caplog.set_level(logging.INFO, logger="gateway.generation")
    result = _run_responses(_entry(True), {"instructions": {"text": "CV"}, "input": [
        {"role": "user", "content": "Quais são minhas certificações?"}]}, monkeypatch)
    assert result["instructions"] == {"text": "CV"}
    # "instructions_not_text" e NÃO "client_instructions": a chave PEDIU a base
    # (flag ligada) e não foi atendida por causa do formato do campo. Rotular
    # isso de bypass legado esconderia, no log, o único caso em que a política
    # ligada não é cumprida — e ninguém iria procurar.
    assert _rag_events(caplog)[-1]["reason"] == "instructions_not_text"


# --------------------------------------------------------------------------
# Higiene do trace
# --------------------------------------------------------------------------

def test_trace_never_carries_content(monkeypatch, caplog):
    """Invariante do generation_trace: metadado, nunca conteúdo. Vale também
    para o caminho novo, que é o primeiro em que o texto do cliente e o chunk
    convivem na mesma decisão."""
    _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    _stub_budget(monkeypatch)
    caplog.set_level(logging.INFO, logger="gateway.generation")
    asyncio.run(main.validate_body({"messages": [
        {"role": "system", "content": "CV FROM CLIENT"},
        {"role": "user", "content": "Quais são minhas certificações?"}], "max_tokens": 2048},
        _entry(True), False, MACHINE, "s1"))
    assert "CV FROM CLIENT" not in caplog.text
    assert "PRIVATE DOCUMENT" not in caplog.text
    assert "certificações" not in caplog.text


# --------------------------------------------------------------------------
# Regressões da revisão adversarial
# --------------------------------------------------------------------------

def test_search_failure_degrades_instead_of_breaking_the_request(monkeypatch, caplog):
    """Busca que estoura não pode derrubar a inferência.

    match_knowledge_chunks faz raise_for_status (supa.py), e sem o try/except a
    exceção subia pelo validate_body inteiro. No proxy genérico isso mandava o
    corpo CRU pro pod (sem model pinado, sem clamp de n, sem piso de
    max_tokens); em /v1/messages vazava o contador in_flight, que segura um
    slot de concorrência e impede a auto-pausa do pod. Uma indisponibilidade do
    Supabase não pode custar isso."""
    async def embedding(text):
        return [1.0]
    async def exploding_search(*args, **kwargs):
        raise RuntimeError("PostgREST 500")
    monkeypatch.setattr(main, "embed_query", embedding)
    _install_supa(monkeypatch, exploding_search)
    caplog.set_level(logging.INFO, logger="gateway.generation")

    result = asyncio.run(main.build_stack_system_message(
        [{"role": "user", "content": "Quais são minhas certificações?"}], _entry(True)))

    # A request sobrevive e mantém o system prompt configurado — só não tem o
    # bloco de contexto.
    assert result["content"] == "Seja breve."
    assert _rag_events(caplog)[-1]["reason"] == "search_failed"


def test_search_failure_keeps_client_system_intact(monkeypatch, caplog):
    """O mesmo, no ramo do system do cliente: as mensagens têm que sair como
    entraram, e o validate_body não pode propagar a exceção."""
    async def embedding(text):
        return [1.0]
    async def exploding_search(*args, **kwargs):
        raise RuntimeError("PostgREST 500")
    monkeypatch.setattr(main, "embed_query", embedding)
    _install_supa(monkeypatch, exploding_search)
    messages = [{"role": "system", "content": "CV FROM CLIENT"},
                {"role": "user", "content": "Quais são minhas certificações?"}]
    _stub_budget(monkeypatch)
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(True), False, MACHINE, "s1"))
    assert result["messages"] == messages


def test_responses_input_as_string_reaches_retrieval(monkeypatch, caplog):
    """`input` como string é forma válida da Responses API. Caía em
    no_user_text e a chave com a base LIGADA não consultava nada — a promessa
    "consulta sempre" falhava só nesse protocolo."""
    seen = _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    caplog.set_level(logging.INFO, logger="gateway.generation")
    instructions = asyncio.run(
        main.build_stack_instructions(_entry(True),
                                      main._last_user_text_from_responses_input("Quais são minhas certificações?"))
    )
    assert seen["text"] == "Quais são minhas certificações?"
    assert "PRIVATE DOCUMENT" in instructions
    assert _rag_events(caplog)[-1]["reason"] == "retrieved"


def test_exactly_one_rag_trace_per_request(monkeypatch, caplog):
    """Um trace por request, nunca dois nem zero — em todas as combinações de
    (política × system do cliente). Os testes existentes liam só o ÚLTIMO
    evento, então um trace duplicado passaria batido."""
    _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    _stub_budget(monkeypatch)
    for flag in (None, True, False):
        for client_system in (True, False):
            caplog.clear()
            caplog.set_level(logging.INFO, logger="gateway.generation")
            messages = ([{"role": "system", "content": "CV FROM CLIENT"}] if client_system else []) + [
                {"role": "user", "content": "Quais são minhas certificações?"}]
            asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
                _entry(flag), False, MACHINE, "s1"))
            events = _rag_events(caplog)
            assert len(events) == 1, (flag, client_system, events)


def test_context_separator_between_client_system_and_chunks(monkeypatch, caplog):
    """Fixa o separador. Sem esta asserção, trocar "\n\n---\n\n" por "\n" não
    quebrava teste nenhum — e o modelo perderia a fronteira entre a instrução
    do cliente e o material recuperado."""
    _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    messages = [{"role": "system", "content": "CV FROM CLIENT"},
                {"role": "user", "content": "Quais são minhas certificações?"}]
    _stub_budget(monkeypatch)
    result = asyncio.run(main.validate_body({"messages": messages, "max_tokens": 2048},
        _entry(True), False, MACHINE, "s1"))
    assert result["messages"][0]["content"] == (
        "CV FROM CLIENT\n\n---\n\nContexto relevante da base de conhecimento:\nPRIVATE DOCUMENT"
    )


def test_enabled_without_client_system_keeps_stack_prompt_and_context(monkeypatch, caplog):
    """Ligada e SEM system do cliente: o prompt da stack e o contexto convivem
    na mesma mensagem, nessa ordem. Não havia cobertura desta combinação."""
    _install_search(monkeypatch, ["PRIVATE DOCUMENT"])
    result = asyncio.run(main.build_stack_system_message(
        [{"role": "user", "content": "Quais são minhas certificações?"}], _entry(True)))
    assert result["content"] == (
        "Seja breve.\n\n---\n\nContexto relevante da base de conhecimento:\nPRIVATE DOCUMENT"
    )
