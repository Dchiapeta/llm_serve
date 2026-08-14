"""Testes do filtro de raciocínio do chat/completions.

    python3 -m pytest test_reasoning_filter.py

Quase tudo aqui roda DUAS vezes, uma por nome de campo ("reasoning" e
"reasoning_content"). Não é zelo: a suíte antiga cobria só o nome velho, e foi
exatamente por isso que ela ficou verde enquanto a produção entregava resposta
vazia. Um teste que conhece um nome só não teria pego o bug e não pegaria a
volta dele.
"""

import json

import pytest

from reasoning_filter import (
    ChatReasoningFilter,
    clean_completion_payload,
    clean_message,
    reasoning_text,
    split_reasoning,
)

NOMES = ["reasoning", "reasoning_content"]


def _sse(delta=None, finish_reason=None, usage=None, choices_vazio=False) -> bytes:
    chunk = {"object": "chat.completion.chunk"}
    if choices_vazio:
        chunk["choices"] = []
    else:
        choice = {"index": 0, "delta": delta or {}}
        if finish_reason:
            choice["finish_reason"] = finish_reason
        chunk["choices"] = [choice]
    if usage:
        chunk["usage"] = usage
    return b"data: " + json.dumps(chunk).encode() + b"\n\n"


def _texto_visivel(saida: list[bytes]) -> str:
    """Concatena o content de todos os chunks emitidos."""
    out = ""
    for linha in b"".join(saida).split(b"\n"):
        linha = linha.strip()
        if not linha.startswith(b"data:"):
            continue
        payload = linha[len(b"data:") :].strip()
        if payload in (b"[DONE]", b""):
            continue
        chunk = json.loads(payload)
        for c in chunk.get("choices") or []:
            out += (c.get("delta") or {}).get("content") or ""
    return out


def _erros(saida: list[bytes]) -> list[dict]:
    out = []
    for linha in b"".join(saida).split(b"\n"):
        linha = linha.strip()
        if not linha.startswith(b"data:"):
            continue
        payload = linha[len(b"data:") :].strip()
        if payload in (b"[DONE]", b""):
            continue
        chunk = json.loads(payload)
        if chunk.get("error"):
            out.append(chunk["error"])
    return out


# ---------- o campo, sob os dois nomes ----------


@pytest.mark.parametrize("nome", NOMES)
def test_raciocinio_nunca_chega_ao_cliente(nome):
    """O raciocínio é privado nos planos filtrados. A versão antiga repassava a
    linha CRUA ao detectar o campo — teria vazado tudo se a guarda funcionasse."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({nome: "segredo do raciocinio"}))
    saida += f.feed(_sse({"content": "resposta"}))
    bruto = b"".join(saida)
    assert b"segredo" not in bruto
    assert nome.encode() not in bruto
    assert _texto_visivel(saida) == "resposta"


@pytest.mark.parametrize("nome", NOMES)
def test_streaming_continua_incremental(nome):
    """O ponto da correção: com o parser ligado o conteúdo sai chunk a chunk.
    Represado, o cliente recebia a resposta inteira de uma vez só no fim."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({nome: "pensando"}))
    chunks = []
    for pedaco in ("Um", " mutex", " serve"):
        emitido = f.feed(_sse({"content": pedaco}))
        if _texto_visivel(emitido):
            chunks.append(_texto_visivel(emitido))
    assert chunks == ["Um", " mutex", " serve"], "conteúdo saiu represado"
    assert _texto_visivel(saida) == ""


@pytest.mark.parametrize("nome", NOMES)
def test_raciocinio_e_conteudo_no_mesmo_delta(nome):
    """A transição costuma vir num delta só. O raciocínio sai, o conteúdo fica."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({nome: "fim do raciocinio", "content": "Olá"}))
    assert b"fim do raciocinio" not in b"".join(saida)
    assert _texto_visivel(saida) == "Olá"


# ---------- o caso que o usuário exigiu que fosse definido ----------


@pytest.mark.parametrize("nome", NOMES)
def test_so_raciocinio_sem_conteudo_nao_vaza_e_nao_finge_sucesso(nome):
    """Modelo gastou o teto de tokens pensando: nenhum content jamais veio.

    As duas saídas erradas são vazar o raciocínio (é privado) e fechar em
    silêncio (o cliente lê como resposta completa e o usuário paga por nada).
    Tem que sair erro explícito, sem o texto do raciocínio."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({nome: "pensando muito"}))
    saida += f.feed(_sse({nome: " e mais"}))
    saida += f.feed(_sse({}, finish_reason="length"))
    saida += f.feed(b"data: [DONE]\n\n")

    bruto = b"".join(saida)
    assert b"pensando muito" not in bruto, "vazou raciocínio privado"
    assert _texto_visivel(saida) == ""
    erros = _erros(saida)
    assert len(erros) == 1, "resposta vazia fechou em silêncio"
    assert erros[0]["code"] == "reasoning_only_response"
    assert f.entregou_visivel is False
    assert f.finish_reason == "length"


@pytest.mark.parametrize("nome", NOMES)
def test_erro_de_vazio_vem_antes_do_done(nome):
    """Quem consome SSE para no [DONE]: erro emitido depois dele é erro que
    ninguém lê."""
    f = ChatReasoningFilter()
    f.feed(_sse({nome: "pensando"}))
    saida = b"".join(f.feed(b"data: [DONE]\n\n"))
    assert saida.index(b"reasoning_only_response") < saida.index(b"[DONE]")


@pytest.mark.parametrize("nome", NOMES)
def test_stream_cortado_sem_done_tambem_avisa(nome):
    """Upstream morreu sem [DONE]: o finish() tem que fechar do mesmo jeito."""
    f = ChatReasoningFilter()
    f.feed(_sse({nome: "pensando"}))
    erros = _erros(f.finish())
    assert len(erros) == 1
    assert erros[0]["code"] == "reasoning_only_response"


@pytest.mark.parametrize("nome", NOMES)
def test_resposta_com_texto_nao_recebe_erro(nome):
    """Espelho do anterior: não inventar erro em resposta que entregou."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({nome: "pensando"}))
    saida += f.feed(_sse({"content": "resposta"}))
    saida += f.feed(b"data: [DONE]\n\n")
    saida += f.finish()
    assert _erros(saida) == []
    assert f.entregou_visivel is True


def test_erro_de_vazio_nao_sai_duas_vezes():
    f = ChatReasoningFilter()
    f.feed(_sse({"reasoning": "pensando"}))
    saida = f.feed(b"data: [DONE]\n\n") + f.finish()
    assert len(_erros(saida)) == 1


@pytest.mark.parametrize("nome", NOMES)
def test_content_antes_do_primeiro_reasoning_nao_e_descartado(nome):
    """Ordem incomum — content chega ANTES do primeiro chunk de raciocínio —
    mas descartar esse buffer comeria resposta do usuário. Tem que ser
    prependado ao delta que traz o campo de raciocínio."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({"content": "inicio"}))
    saida += f.feed(_sse({nome: "pensando", "content": " meio"}))
    saida += f.feed(_sse({"content": " fim"}))
    assert _texto_visivel(saida) == "inicio meio fim"
    assert b"pensando" not in b"".join(saida)


@pytest.mark.parametrize("nome", NOMES)
def test_buffer_anterior_sobrevive_a_chunk_de_raciocinio_puro(nome):
    """Variante: o chunk de raciocínio não traz content nenhum. O buffer
    anterior não pode sumir junto com o frame suprimido."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({"content": "inicio"}))
    saida += f.feed(_sse({nome: "pensando"}))
    saida += f.feed(_sse({"content": " fim"}))
    assert _texto_visivel(saida) == "inicio fim"


def test_finish_sem_erro_vazio_nao_emite_reasoning_only():
    """Quando o chamador já vai emitir o erro da causa real (timeout/queda),
    o filtro não pode emitir o dele: dois erros terminais se contradizem."""
    f = ChatReasoningFilter()
    f.feed(_sse({"reasoning": "pensando"}))
    saida = f.finish(emitir_erro_vazio=False)
    assert _erros(saida) == []


@pytest.mark.parametrize("nome", NOMES)
def test_erro_substitui_o_chunk_terminal_vazio(nome):
    """UM terminal só. O chunk de finish_reason sem conteúdo não pode sair junto
    com o erro: seriam dois sinais terminais dizendo coisas diferentes, e o
    cliente fecha o turno no primeiro que entender."""
    f = ChatReasoningFilter()
    f.feed(_sse({nome: "pensando"}))
    bruto = b"".join(f.feed(_sse({}, finish_reason="length")))
    assert b"reasoning_only_response" in bruto
    assert b"finish_reason" not in bruto, "chunk terminal vazio saiu junto com o erro"


@pytest.mark.parametrize("nome", NOMES)
def test_finish_reason_normal_continua_passando(nome):
    """Espelho: quando HOUVE entrega, o chunk terminal é legítimo e passa."""
    f = ChatReasoningFilter()
    f.feed(_sse({nome: "pensando"}))
    f.feed(_sse({"content": "resposta"}))
    bruto = b"".join(f.feed(_sse({}, finish_reason="stop")))
    assert b"finish_reason" in bruto
    assert b"reasoning_only_response" not in bruto


# ---------- tool calling ----------

TOOL = [{"index": 0, "id": "call_1", "type": "function",
         "function": {"name": "ler_arquivo", "arguments": "{}"}}]


@pytest.mark.parametrize("nome", NOMES)
def test_tool_call_depois_do_raciocinio_nao_vira_resposta_vazia(nome):
    """Resposta só de tool_calls NÃO tem content — é assim por construção.
    Contar isso como vazia encheria de erro espúrio o caminho do Claude Code,
    que é quase todo tool call."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({nome: "vou ler o arquivo"}))
    saida += f.feed(_sse({"tool_calls": TOOL}))
    saida += f.feed(_sse({}, finish_reason="tool_calls"))
    saida += f.feed(b"data: [DONE]\n\n")

    assert _erros(saida) == [], "tool call foi tratado como resposta vazia"
    assert f.entregou_visivel is True
    assert b"vou ler o arquivo" not in b"".join(saida)
    assert b"ler_arquivo" in b"".join(saida)


def test_tool_call_sem_parser_nao_fica_represado():
    """Sem parser de raciocínio o modelo pode ir direto pro tool call sem nunca
    emitir </think> — não pode ficar preso no buffer até o fim do stream."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({"tool_calls": TOOL}))
    assert b"ler_arquivo" in b"".join(saida)
    assert f.entregou_visivel is True


# ---------- regime antigo: raciocínio embutido com tags ----------


def test_sem_parser_o_think_embutido_continua_filtrado():
    """Template sem ENABLE_REASONING_PARSER: o raciocínio vem no content com as
    tags e o filtro precisa buferizar até o </think>."""
    f = ChatReasoningFilter()
    saida = f.feed(_sse({"content": "raciocinando aqui"}))
    assert _texto_visivel(saida) == "", "raciocínio vazou antes do </think>"
    saida = f.feed(_sse({"content": "</think>\n\nResposta final"}))
    assert _texto_visivel(saida) == "Resposta final"


def test_com_thinking_o_buffer_sem_think_close_e_descartado():
    """MUDANÇA DE CONTRATO. Antes o buffer sem </think> era entregue ("melhor
    resposta estranha que nenhuma"). Mas com thinking ligado tudo antes do
    </think> é o raciocínio que o filtro existe pra esconder — entregar trocava
    resposta vazia por VAZAMENTO."""
    f = ChatReasoningFilter(thinking_esperado=True)
    f.feed(_sse({"content": "raciocinio privado"}))
    saida = f.feed(_sse({}, finish_reason="length"))
    assert "raciocinio privado" not in b"".join(saida).decode()
    assert len(_erros(saida)) == 1


def test_sem_thinking_o_mesmo_texto_E_a_resposta_e_sai_na_hora():
    """Espelho do anterior, e a razão de `thinking_esperado` existir.

    O modelo NÃO emite a tag de abertura (o chat template já a injeta), então
    texto sem </think> é indistinguível de resposta comum olhando só o
    conteúdo. Com thinking desligado — o gateway força isso quando max_tokens <
    MIN_MAX_TOKENS — a resposta legítima não tem tag nenhuma, e descartá-la
    comeria toda resposta curta do plano.

    E ela sai NO CHUNK EM QUE CHEGA: represar aqui esperava um </think> que
    nunca viria, e o texto só aparecia no fim do stream."""
    f = ChatReasoningFilter(thinking_esperado=False)
    primeiro = f.feed(_sse({"content": "resposta curta"}))
    assert _texto_visivel(primeiro) == "resposta curta", "conteúdo saiu represado"
    saida = f.feed(_sse({}, finish_reason="length"))
    assert _erros(primeiro + saida) == []


def test_sem_thinking_o_conteudo_sai_incremental_chunk_a_chunk():
    """Dois chunks, dois frames emitidos — não um só no fim.

    A regressão que isto tranca: `_in_reasoning` começava True mesmo sem
    thinking pedido, então o primeiro chunk (e todos os seguintes) ficava no
    buffer até o fim do stream, esperando um </think> que o modelo não ia
    emitir. O cliente recebia a resposta inteira de uma vez."""
    f = ChatReasoningFilter(thinking_esperado=False)
    saidos = [_texto_visivel(f.feed(_sse({"content": p}))) for p in ("Ola", " mundo")]
    assert saidos == ["Ola", " mundo"]


def test_sem_thinking_stream_cortado_no_meio_preserva_o_que_ja_saiu():
    """O texto já saiu no feed; o finish() não pode inventar erro de vazio por
    cima de uma resposta que foi entregue."""
    f = ChatReasoningFilter(thinking_esperado=False)
    entregue = f.feed(_sse({"content": "parcial"}))
    saida = f.finish()
    assert _texto_visivel(entregue) == "parcial"
    assert _texto_visivel(saida) == ""
    assert _erros(saida) == []
    assert f.entregou_visivel is True


def test_com_thinking_stream_cortado_no_meio_nao_vaza():
    f = ChatReasoningFilter(thinking_esperado=True)
    f.feed(_sse({"content": "raciocinio interrompido"}))
    saida = f.finish()
    assert "raciocinio interrompido" not in b"".join(saida).decode()
    assert len(_erros(saida)) == 1


# ---------- plumbing ----------


def test_linha_em_branco_e_repassada_como_keepalive():
    """As linhas em branco dos frames suprimidos seguram a conexão durante um
    raciocínio longo. Sem elas o cliente veria silêncio total até o fim."""
    f = ChatReasoningFilter()
    saida = b"".join(f.feed(_sse({"reasoning": "pensando"})))
    assert saida == b"\n", "sumiu o keepalive do frame suprimido"


def test_chunk_partido_entre_dois_feeds():
    """SSE chega fatiado por TCP; a linha pode quebrar no meio do JSON."""
    inteiro = _sse({"reasoning": "x", "content": "resposta"})
    corte = len(inteiro) // 2
    f = ChatReasoningFilter()
    saida = f.feed(inteiro[:corte]) + f.feed(inteiro[corte:])
    assert _texto_visivel(saida) == "resposta"


def test_usage_e_capturado_do_chunk_final():
    """usage vem num chunk com choices vazio, depois do conteúdo — se o filtro
    o engolir, tokens_in/tokens_out ficam null pra sempre nos planos filtrados."""
    f = ChatReasoningFilter()
    f.feed(_sse({"content": "oi"}))
    f.feed(_sse(choices_vazio=True, usage={"prompt_tokens": 10, "completion_tokens": 2}))
    assert f.usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_linha_nao_json_passa_intacta():
    f = ChatReasoningFilter()
    assert f.feed(b"data: nao-e-json\n\n")[0] == b"data: nao-e-json\n"


@pytest.mark.parametrize("nome", NOMES)
def test_reasoning_text_reconhece_os_dois_nomes(nome):
    assert reasoning_text({nome: "abc"}) == "abc"
    assert reasoning_text({"content": "abc"}) is None
    # campo presente porém vazio não é "ausente": o vLLM manda esses no começo
    assert reasoning_text({nome: None}) == ""


def test_split_reasoning_sem_tag_devolve_o_original():
    assert split_reasoning("sem tag") == (None, "sem tag")
    assert split_reasoning("pensei</think>\n\nresposta") == ("pensei", "resposta")


# ---------- não-streamed ----------


@pytest.mark.parametrize("nome", NOMES)
def test_clean_message_remove_o_campo_e_detecta_vazio(nome):
    """O caminho não-streamed sofre do MESMO modo de falha: com o teto gasto no
    raciocínio, content vem null e a guarda antiga (isinstance str) pulava,
    entregando resposta vazia."""
    limpa, visivel = clean_message({"role": "assistant", "content": None, nome: "pensei muito"})
    assert nome not in limpa
    assert visivel is False

    limpa, visivel = clean_message({"role": "assistant", "content": "resposta", nome: "pensei"})
    assert limpa["content"] == "resposta"
    assert nome not in limpa
    assert visivel is True


def test_clean_message_ainda_corta_o_think_embutido():
    limpa, visivel = clean_message({"content": "pensei</think>\n\nresposta"})
    assert limpa["content"] == "resposta"
    assert visivel is True


def test_clean_message_com_thinking_e_sem_think_close_nao_entrega_o_bruto():
    """Não-streamed, parser DESLIGADO, thinking ligado e nenhum </think>: o
    content inteiro é raciocínio. Devolvê-lo cru entrega exatamente o que o
    filtro existe pra esconder."""
    limpa, visivel = clean_message(
        {"role": "assistant", "content": "raciocinio privado sem tag"},
        True,
    )
    assert limpa["content"] == ""
    assert visivel is False


def test_clean_message_sem_thinking_preserva_o_mesmo_texto():
    """Espelho: o MESMO texto com thinking desligado é a resposta legítima."""
    limpa, visivel = clean_message(
        {"role": "assistant", "content": "resposta curta sem tag"},
        False,
    )
    assert limpa["content"] == "resposta curta sem tag"
    assert visivel is True


def test_clean_message_com_parser_ligado_nao_descarta_content_limpo():
    """Com o parser separando o raciocínio, o content JÁ vem limpo e sem
    </think> — não pode ser confundido com raciocínio bruto."""
    limpa, visivel = clean_message(
        {"role": "assistant", "content": "resposta limpa", "reasoning": "pensei"},
        True,
    )
    assert limpa["content"] == "resposta limpa"
    assert visivel is True


@pytest.mark.parametrize("status", [400, 401, 429, 500, 502])
def test_erro_upstream_nunca_vira_resposta_vazia(status):
    """Corpo de erro do upstream não tem "choices". Sem a guarda de status ele
    seria classificado como resposta-só-raciocínio, e o erro REAL — status e
    mensagem — sairia trocado por um genérico nosso, apagando a causa."""
    corpo = {"error": {"message": "prompt is too long", "type": "invalid_request_error"}}
    payload, vazia = clean_completion_payload(dict(corpo), status_code=status)
    assert vazia is False, "erro do upstream classificado como resposta vazia"
    assert payload == corpo, "corpo do erro upstream foi alterado"


def test_resposta_200_so_de_raciocinio_e_vazia():
    payload, vazia = clean_completion_payload(
        {"choices": [{"message": {"role": "assistant", "content": None, "reasoning": "pensei"}}]},
        status_code=200,
    )
    assert vazia is True
    assert "reasoning" not in payload["choices"][0]["message"]


def test_resposta_200_com_texto_nao_e_vazia():
    payload, vazia = clean_completion_payload(
        {"choices": [{"message": {"role": "assistant", "content": "oi", "reasoning": "pensei"}}]},
        status_code=200,
    )
    assert vazia is False
    assert payload["choices"][0]["message"]["content"] == "oi"


def test_nao_streamed_thinking_ligado_sem_parser_e_sem_think_close():
    """Parser desligado, thinking ligado, nenhum </think>: content inteiro é
    raciocínio. Vira resposta vazia em vez de vazar o texto bruto."""
    payload, vazia = clean_completion_payload(
        {"choices": [{"message": {"role": "assistant", "content": "raciocinio cru"}}]},
        status_code=200,
        thinking_esperado=True,
    )
    assert vazia is True
    assert payload["choices"][0]["message"]["content"] == ""


def test_nao_streamed_thinking_desligado_preserva_o_mesmo_texto():
    payload, vazia = clean_completion_payload(
        {"choices": [{"message": {"role": "assistant", "content": "resposta curta"}}]},
        status_code=200,
        thinking_esperado=False,
    )
    assert vazia is False
    assert payload["choices"][0]["message"]["content"] == "resposta curta"


def test_clean_message_tool_call_sem_texto_conta_como_entrega():
    tool = [{"id": "c1", "type": "function", "function": {"name": "ler", "arguments": "{}"}}]
    limpa, visivel = clean_message(
        {"role": "assistant", "content": None, "tool_calls": tool, "reasoning": "pensei"},
        True,
    )
    assert visivel is True
    assert "reasoning" not in limpa
