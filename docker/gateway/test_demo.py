"""Testes da política da demo pública (demo.py) — módulo puro.

    python3 -m pytest test_demo.py

Os casos aqui são os que protegem um endpoint sem autenticação: input recusado,
janela de rate limit que não vaza, origem não autorizada e raciocínio do modelo
que não pode aparecer na landing page.
"""

import json

import pytest

from demo import (
    MAX_INPUT_CHARS,
    MAX_TOKENS,
    SYSTEM_PROMPT,
    InvalidPrompt,
    SlidingWindowLimiter,
    VisibleText,
    build_payload,
    cors_headers,
    parse_origins,
    sse_delta,
    validate_prompt,
)


def _sse(**delta) -> bytes:
    """Uma linha SSE de chat completion, como o vLLM manda."""
    chunk = {"choices": [{"index": 0, "delta": delta}]}
    return b"data: " + json.dumps(chunk).encode() + b"\n"


# ---------- validate_prompt ----------


def test_prompt_valido_volta_limpo():
    assert validate_prompt("  como faço streaming?  ") == "como faço streaming?"


def test_prompt_normaliza_crlf_antes_de_medir():
    """Sem isso, um prompt colado do editor conta \\r\\n como 2 caracteres e é
    recusado por um limite que ele não estourou de verdade."""
    texto = "a\r\nb"
    assert validate_prompt(texto) == "a\nb"


@pytest.mark.parametrize("raw", [None, 42, {"prompt": "x"}, ["x"], True])
def test_prompt_precisa_ser_texto(raw):
    with pytest.raises(InvalidPrompt):
        validate_prompt(raw)


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_prompt_vazio_e_recusado(raw):
    with pytest.raises(InvalidPrompt):
        validate_prompt(raw)


def test_prompt_no_limite_exato_passa():
    assert len(validate_prompt("x" * MAX_INPUT_CHARS)) == MAX_INPUT_CHARS


def test_prompt_acima_do_limite_e_recusado_nao_truncado():
    """Truncar devolveria a resposta de uma pergunta diferente da que a pessoa
    fez — do lado do terminal isso pareceria alucinação do modelo."""
    with pytest.raises(InvalidPrompt):
        validate_prompt("x" * (MAX_INPUT_CHARS + 1))


def test_espaco_nas_pontas_nao_conta_pro_limite():
    assert validate_prompt("  " + "x" * MAX_INPUT_CHARS + "  ")


# ---------- build_payload ----------


def test_payload_trava_saida_e_injeta_o_system_prompt():
    payload = build_payload("como faço streaming?", "demo-base")
    assert payload["max_tokens"] == MAX_TOKENS
    assert payload["model"] == "demo-base"
    assert payload["stream"] is True
    assert payload["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert payload["messages"][1] == {"role": "user", "content": "como faço streaming?"}
    # teto baixo + thinking ligado = os 80 tokens vão embora no raciocínio e a
    # resposta visível sai vazia (mesmo motivo de /v1/documents/generate)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_payload_e_montado_do_zero_sem_nada_do_cliente():
    """Teste-guarda do contrato de um campo só: se alguém adicionar um
    parâmetro vindo do corpo da request, este teste cai. Nada além de `prompt`
    pode influenciar o que vai pro pod."""
    payload = build_payload("oi", "demo-base")
    assert set(payload) == {
        "model",
        "messages",
        "max_tokens",
        "temperature",
        "top_p",
        "stream",
        "chat_template_kwargs",
    }


def test_system_prompt_cobre_escopo_tamanho_e_resistencia():
    """O prompt é a única barreira de comportamento de um endpoint anônimo —
    as três regras não podem ser perdidas numa reescrita de copy."""
    texto = SYSTEM_PROMPT.lower()
    assert "only" in texto and "programming" in texto      # escopo
    assert "2 short sentences" in texto                    # tamanho
    assert "ignore" in texto and "persona" in texto        # resistência


# ---------- SlidingWindowLimiter ----------


def test_limiter_libera_ate_o_teto_e_recusa_o_seguinte():
    limiter = SlidingWindowLimiter(limit=5, window_s=3600)
    for i in range(5):
        assert limiter.take("ip", now=1000 + i) is None
    assert limiter.take("ip", now=1005) is not None


def test_limiter_libera_de_novo_quando_o_primeiro_hit_expira():
    limiter = SlidingWindowLimiter(limit=2, window_s=100)
    assert limiter.take("ip", now=0) is None
    assert limiter.take("ip", now=50) is None
    assert limiter.take("ip", now=99) is not None
    assert limiter.take("ip", now=101) is None   # o hit de t=0 saiu da janela


def test_limiter_recusado_nao_empurra_a_janela():
    """Quem insiste no F5 tem que conseguir entrar quando a vaga abre: se a
    tentativa recusada gravasse um hit, a janela nunca terminaria."""
    limiter = SlidingWindowLimiter(limit=1, window_s=100)
    assert limiter.take("ip", now=0) is None
    for t in range(1, 100):
        assert limiter.take("ip", now=t) is not None
    assert limiter.take("ip", now=101) is None


def test_limiter_retry_after_e_o_tempo_ate_a_vaga():
    limiter = SlidingWindowLimiter(limit=1, window_s=3600)
    limiter.take("ip", now=0)
    assert limiter.take("ip", now=600) == pytest.approx(3000)


def test_limiter_retry_after_nunca_e_zero():
    """Retry-After: 0 convida o cliente a tentar no mesmo instante — sempre
    sobra pelo menos 1 segundo."""
    limiter = SlidingWindowLimiter(limit=1, window_s=100)
    limiter.take("ip", now=0)
    assert limiter.take("ip", now=99.999) >= 1.0


def test_limiter_isola_as_chaves():
    limiter = SlidingWindowLimiter(limit=1, window_s=100)
    assert limiter.take("a", now=0) is None
    assert limiter.take("b", now=0) is None
    assert limiter.take("a", now=1) is not None


def test_limiter_esquece_chave_antiga():
    """Sem a varredura, o dict guarda uma deque por visitante do site para
    sempre — vazamento proporcional ao tráfego que a gente quer atrair."""
    limiter = SlidingWindowLimiter(limit=1, window_s=10)
    for i in range(SlidingWindowLimiter._PURGE_EVERY + 1):
        limiter.take(f"ip-{i}", now=1000 + i)
    assert len(limiter._hits) < SlidingWindowLimiter._PURGE_EVERY


# ---------- CORS ----------


def test_parse_origins_aceita_csv_e_normaliza_barra_final():
    assert parse_origins("https://trystac.com/, https://www.trystac.com") == frozenset(
        {"https://trystac.com", "https://www.trystac.com"}
    )


@pytest.mark.parametrize("raw", [None, "", "  ", ","])
def test_parse_origins_vazio_e_conjunto_vazio(raw):
    assert parse_origins(raw) == frozenset()


def test_cors_permite_origem_da_lista():
    allowed = parse_origins("https://trystac.com")
    headers = cors_headers("https://trystac.com", allowed)
    assert headers["Access-Control-Allow-Origin"] == "https://trystac.com"
    # sem Vary, um cache compartilhado serve o Allow-Origin de outro visitante
    assert headers["Vary"] == "Origin"


def test_cors_recusa_origem_fora_da_lista():
    allowed = parse_origins("https://trystac.com")
    assert cors_headers("https://evil.example", allowed) is None
    assert cors_headers("https://trystac.com.evil.example", allowed) is None


def test_cors_sem_origem_passa_sem_headers():
    """curl, teste de fumaça e monitor não têm browser pra proteger."""
    assert cors_headers(None, parse_origins("https://trystac.com")) == {}


def test_cors_lista_vazia_recusa_qualquer_browser():
    """Fail-closed: deploy sem a env var configurada não vira demo aberta pra
    qualquer site embutir."""
    assert cors_headers("https://trystac.com", frozenset()) is None


def test_preflight_declara_metodo_e_header_e_recusa_sem_origem():
    allowed = parse_origins("https://trystac.com")
    headers = cors_headers("https://trystac.com", allowed, preflight=True)
    assert headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
    assert headers["Access-Control-Allow-Headers"] == "Content-Type"
    assert cors_headers(None, allowed, preflight=True) is None


# ---------- VisibleText ----------


def _read(stream: VisibleText, *chunks: bytes) -> str:
    out = []
    for chunk in chunks:
        out.extend(stream.feed(chunk))
    out.extend(stream.finish())
    return "".join(out)


def test_texto_simples_atravessa_em_pedacos():
    stream = VisibleText()
    assert stream.feed(_sse(content="Set ")) == ["Set "]
    assert stream.feed(_sse(content="base_url.")) == ["base_url."]


def test_linha_partida_no_meio_do_json_nao_perde_texto():
    """TCP não respeita fronteira de linha SSE — o chunk pode cortar o JSON no
    meio, e antes do buffer isso virava delta perdido em silêncio."""
    raw = _sse(content="tokens")
    stream = VisibleText()
    assert _read(stream, raw[:12], raw[12:]) == "tokens"


def test_done_e_marcado_e_nao_vira_texto():
    stream = VisibleText()
    assert stream.feed(b"data: [DONE]\n") == []
    assert stream.done is True


def test_chunk_de_usage_e_linha_vazia_sao_ignorados():
    stream = VisibleText()
    assert stream.feed(b'data: {"choices": [], "usage": {"total_tokens": 9}}\n') == []
    assert stream.feed(b"\n") == []
    assert stream.feed(b": keep-alive\n") == []


def test_json_quebrado_nao_derruba_o_stream():
    stream = VisibleText()
    assert stream.feed(b"data: {nao json\n") == []
    assert stream.feed(_sse(content="ok")) == ["ok"]


def test_reasoning_content_nunca_vira_texto_visivel():
    """vLLM com --reasoning-parser separa o raciocínio nesse campo; a demo só
    lê `content`, então ele é ignorado por construção."""
    stream = VisibleText()
    assert stream.feed(_sse(reasoning_content="o usuário quer...")) == []
    assert stream.feed(_sse(content="Resposta.")) == ["Resposta."]


def test_think_embutido_no_content_e_suprimido():
    stream = VisibleText()
    texto = _read(
        stream,
        _sse(content="<think>"),
        _sse(content="pensando alto"),
        _sse(content="</think>"),
        _sse(content="\nSet base_url."),
    )
    assert texto == "Set base_url."


def test_quebra_de_linha_pos_think_e_engolida_mesmo_em_outro_delta():
    """Na prática o </think> vem num delta e o "\\n\\n" no seguinte. Limpar só
    dentro do delta que fecha a tag deixaria a resposta começando com uma linha
    em branco — visível na hero, que tem altura fixa."""
    stream = VisibleText()
    texto = _read(
        stream,
        _sse(content="<think>x</think>"),
        _sse(content="\n\n"),
        _sse(content="Set base_url."),
    )
    assert texto == "Set base_url."


def test_tag_think_fatiada_entre_deltas_nao_aparece():
    """"<th" + "ink>" é fatiamento normal de token — segurar o começo do texto
    até dar pra decidir é o que impede a tag de aparecer na hero."""
    stream = VisibleText()
    texto = _read(
        stream,
        _sse(content="<th"),
        _sse(content="ink>x</think>"),
        _sse(content="Pronto."),
    )
    assert texto == "Pronto."


def test_texto_que_apenas_comeca_parecido_com_a_tag_e_preservado():
    stream = VisibleText()
    assert _read(stream, _sse(content="<t"), _sse(content="able> em HTML")) == (
        "<table> em HTML"
    )


def test_raciocinio_nunca_fechado_e_liberado_no_fim():
    """Se os 80 tokens acabam com o </think> ainda por vir, mostrar o texto
    represado é melhor que uma tela vazia — mesma decisão do filtro de
    raciocínio do proxy (main.py)."""
    stream = VisibleText()
    assert _read(stream, _sse(content="<think>sem fechar")) == "sem fechar"


def test_resposta_curta_sem_quebra_de_linha_final_e_entregue():
    """Última linha sem \\n só sai no finish() — sem ele, respostas de uma
    palavra desapareciam."""
    stream = VisibleText()
    raw = _sse(content="Sim.").rstrip(b"\n")
    assert _read(stream, raw) == "Sim."


# ---------- formato do evento ----------


def test_sse_delta_e_uma_linha_data_com_evento_terminado():
    assert sse_delta("oi") == b'data: {"delta": "oi"}\n\n'


def test_sse_delta_preserva_acento_e_escapa_quebra_de_linha():
    """ensure_ascii=False mantém o UTF-8 legível; o \\n vai escapado no JSON e
    portanto nunca fecha o evento SSE no meio do texto."""
    assert sse_delta("ação\n") == b'data: {"delta": "a\xc3\xa7\xc3\xa3o\\n"}\n\n'
