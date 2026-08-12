"""Testes do orçamento de contexto (context_budget.py) — funções puras, sem
precisar das env vars do main.py nem de rede. Rodar de docker/gateway/:

    python3 -m pytest test_context_budget.py
"""

import math

import pytest

from context_budget import (
    CONTEXT_EXACT_SAFETY_FACTOR,
    CONTEXT_FALLBACK_SAFETY_FACTOR,
    CONTEXT_IMAGE_TOKENS,
    CONTEXT_OVERSHOOT_MARGIN,
    CONTEXT_SAFETY_FACTOR,
    CONTEXT_TEMPLATE_OVERHEAD,
    RESERVED_OUTPUT_TOKENS,
    ContextWindowExceeded,
    EstimateKind,
    admission_ceiling,
    anthropic_error_body,
    apply_context_budget,
    auto_compact_window,
    error_body_for,
    context_exceeded_message,
    count_images,
    estimate_prompt_tokens,
    openai_error_body,
    prompt_text_for_tokenize,
    reserved_tokens_for,
    should_use_exact_token_count,
    usable_input_tokens,
)

GO_WINDOW = 16384


def _machine(max_model_len=GO_WINDOW):
    return {"id": "m1", "max_model_len": max_model_len}


def test_clampa_abaixo_do_piso_quando_a_janela_exige():
    # cenário real do bug: Claude Code pede 16000 de saída numa janela de
    # 16384 — o clamp precisa reduzir pra menos que o piso de 8000, senão o
    # piso re-infla e o vLLM devolve o 400 cru
    prompt = "x" * 20_000  # ~5k tokens estimados
    body = {"max_tokens": 16_000, "messages": [{"role": "user", "content": prompt}]}
    est = estimate_prompt_tokens(messages=body["messages"])
    apply_context_budget(body, _machine(), est_tokens=est)
    expected_budget = GO_WINDOW - reserved_tokens_for(est)
    assert body["max_tokens"] <= expected_budget
    assert body["max_tokens"] < 16_000


def test_prompt_maior_que_a_janela_rejeita_com_erro_claro():
    prompt = "x" * 120_000  # ~30k tokens > janela de 16384
    body = {"max_tokens": 8_000, "messages": [{"role": "user", "content": prompt}]}
    est = estimate_prompt_tokens(messages=body["messages"])
    with pytest.raises(ContextWindowExceeded) as exc:
        apply_context_budget(body, _machine(), est_tokens=est)
    assert exc.value.status_code == 400
    assert "janela de contexto" in exc.value.detail
    assert "16384" in exc.value.detail


def test_sem_max_model_len_nao_toca_no_body():
    # pod anterior à migration 0031 ou template sem --max-model-len: sem
    # clamp, comportamento antigo preservado
    body = {"max_tokens": 16_000, "messages": [{"role": "user", "content": "x" * 200_000}]}
    for machine in ({"id": "m1"}, _machine(None), _machine(0)):
        apply_context_budget(body, machine, est_tokens=999_999)
        assert body["max_tokens"] == 16_000


def test_max_tokens_dentro_do_orcamento_fica_intocado():
    body = {"max_tokens": 1_000}
    apply_context_budget(body, _machine(), est_tokens=100)
    assert body["max_tokens"] == 1_000


def test_campo_ausente_nao_quebra_nem_cria():
    # embeddings e afins não têm max_tokens — clamp vira no-op
    body = {"input": "abc"}
    apply_context_budget(body, _machine(), est_tokens=100)
    assert "max_tokens" not in body


def test_campo_da_responses_api():
    body = {"max_output_tokens": 16_000}
    apply_context_budget(body, _machine(), field="max_output_tokens", est_tokens=3_000)
    assert body["max_output_tokens"] < 16_000


def test_tools_aumentam_a_estimativa():
    messages = [{"role": "user", "content": "oi"}]
    tools = [{"type": "function", "function": {"name": "grep", "description": "x" * 8_000}}]
    assert estimate_prompt_tokens(messages=messages, tools=tools) > estimate_prompt_tokens(
        messages=messages
    )


def _messages_with_images(n: int):
    fake_b64 = "A" * 100_000
    parts = [{"type": "text", "text": "descreva"}]
    for _ in range(n):
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{fake_b64}"}}
        )
    return [{"role": "user", "content": parts}]


def test_base64_da_imagem_fica_fora_da_estimativa_de_texto():
    """O base64 a 4 chars/token contaria ~25k tokens por imagem — absurdo, uma
    imagem não tokeniza como texto."""
    est_with = estimate_prompt_tokens(messages=_messages_with_images(1))
    est_without = estimate_prompt_tokens(messages=_messages_with_images(0))
    assert est_with - est_without < 25_000


def test_imagem_custa_CONTEXT_IMAGE_TOKENS_e_nao_zero():
    """Contar zero era o comportamento anterior e subestimava a janela: num
    modelo VL a imagem ocupa contexto de verdade, e o prompt perto do teto
    passava pelo orçamento pra voltar como 400 do vLLM — que, num cliente que
    reenvia a conversa toda, envenena a sessão inteira."""
    base = estimate_prompt_tokens(messages=_messages_with_images(0))
    uma = estimate_prompt_tokens(messages=_messages_with_images(1))
    tres = estimate_prompt_tokens(messages=_messages_with_images(3))
    assert uma - base == CONTEXT_IMAGE_TOKENS
    assert tres - base == 3 * CONTEXT_IMAGE_TOKENS


def test_prompt_text_for_tokenize_nao_leva_o_base64():
    """O mesmo texto vai pro /tokenize do vLLM perto do limite; mandar 100k de
    base64 pra lá seria uma chamada de rede enorme e uma contagem errada."""
    texto = prompt_text_for_tokenize(messages=_messages_with_images(2))
    assert "base64" not in texto and "AAAA" not in texto
    assert "descreva" in texto


def test_reserved_tokens_for_aplica_fator_e_overhead():
    est = 1_000
    assert reserved_tokens_for(est) == int(est * CONTEXT_SAFETY_FACTOR + 0.999999) + CONTEXT_TEMPLATE_OVERHEAD


def test_reserved_tokens_for_por_tipo_de_estimativa():
    """Cada procedência tem margem própria: 1.2 pra chute, ~1.02 pra contagem
    do tokenizer, meio-termo quando a contagem foi tentada e falhou."""
    est = 10_000
    for kind, factor in (
        (EstimateKind.HEURISTIC, CONTEXT_SAFETY_FACTOR),
        (EstimateKind.EXACT, CONTEXT_EXACT_SAFETY_FACTOR),
        (EstimateKind.FALLBACK, CONTEXT_FALLBACK_SAFETY_FACTOR),
    ):
        esperado = math.ceil(est * factor) + CONTEXT_TEMPLATE_OVERHEAD
        assert reserved_tokens_for(est, kind) == esperado
    # default preservado pra quem não passa kind (should_use_exact_token_count)
    assert reserved_tokens_for(est) == reserved_tokens_for(est, EstimateKind.HEURISTIC)
    # ordenação: exata é a mais folgada, heurística a mais apertada
    assert (
        reserved_tokens_for(est, EstimateKind.EXACT)
        < reserved_tokens_for(est, EstimateKind.FALLBACK)
        < reserved_tokens_for(est, EstimateKind.HEURISTIC)
    )


CLAUDE_CODE_WINDOW = 131_072  # Go e Pro, padronizados


def test_contagem_exata_nao_rejeita_prompt_que_cabe():
    """Regressão do incidente que quebrava o /compact do Claude Code: 122658
    tokens EXATOS numa janela de 131072 cabem com ~8k de sobra pra resposta, mas
    o fator 1.2 (de estimativa) reservava 147390 e o gateway rejeitava. Como
    /compact reenvia a transcrição inteira, era justamente o pedido que morria —
    e a mensagem de erro mandava usar /compact, fechando o beco sem saída."""
    body = {"max_tokens": 8_000}
    budget = apply_context_budget(
        body, _machine(CLAUDE_CODE_WINDOW), est_tokens=122_658, kind=EstimateKind.EXACT
    )
    esperado = CLAUDE_CODE_WINDOW - reserved_tokens_for(122_658, EstimateKind.EXACT)
    assert budget.output_budget == esperado > 0
    assert body["max_tokens"] == esperado  # clampado, não rejeitado


def test_mesmo_prompt_ainda_e_rejeitado_quando_e_so_estimativa():
    """O outro lado: sem contagem do tokenizer o número é um chute e a margem
    de 1.2 continua valendo — comportamento antigo preservado."""
    with pytest.raises(ContextWindowExceeded):
        apply_context_budget(
            {"max_tokens": 8_000},
            _machine(CLAUDE_CODE_WINDOW),
            est_tokens=122_658,
            kind=EstimateKind.HEURISTIC,
        )


def test_janela_recomendada_ao_cliente():
    """Contrato replicado em lib/context-window.ts (o painel gera o
    CLAUDE_CODE_AUTO_COMPACT_WINDOW do snippet a partir da mesma conta).
    Mudou aqui? Atualize lá."""
    assert auto_compact_window(CLAUDE_CODE_WINDOW) == 104_000
    assert auto_compact_window(65_536) == 42_000  # janela do Pro antes de padronizar
    # nunca acima do que o gateway aceita — recomendar mais que isso recriaria o
    # 400 de contexto que o módulo existe pra evitar
    for window in (GO_WINDOW, 65_536, CLAUDE_CODE_WINDOW):
        assert auto_compact_window(window) <= usable_input_tokens(window)


def test_janela_pequena_nao_zera_a_recomendacao():
    """A margem de transbordo é fixa (16k) mas o espaço útil de uma janela de
    16384 é ~8k — subtrair direto devolvia ZERO, e declarar
    CLAUDE_CODE_AUTO_COMPACT_WINDOW=0 é pior que não declarar nada. Nessas
    janelas a margem degrada proporcionalmente
    (CONTEXT_OVERSHOOT_MAX_FRACTION)."""
    for window in (GO_WINDOW, 32_768, 65_536, CLAUDE_CODE_WINDOW):
        declarado = auto_compact_window(window)
        assert declarado > 0, window
        # e continua sobrando margem de verdade até o teto de admissão
        assert admission_ceiling(window) - declarado * 0.92 > 0, window


def test_janela_recomendada_reserva_o_transbordo_de_um_turno():
    """O motivo de NÃO declarar o máximo admissível (que seria 120000 em
    131072). O cliente decide compactar olhando o contexto do turno ANTERIOR;
    entre a decisão e a request seguinte cabe um tool_result inteiro, e um Read
    de arquivo de 60 KB são ~18k tokens.

    Declarando 120000, do topo da banda de compactação (0.92 * 120000 = 110400)
    até o teto de admissão (128054) sobravam 17654 tokens — menos que um único
    arquivo grande. Foi o incidente de 128465 tokens: transbordo de 411 tokens
    sobre o teto, com a sessão morta porque /compact reenvia tudo e é justamente
    o pedido que estoura.

    Este teste substitui test_janela_recomendada_nao_e_80_por_cento_da_janela,
    que fixava o comportamento antigo. O argumento de lá (declarar 80% deixa
    ~15% de contexto na mesa) não estava errado, estava incompleto: otimizava
    aproveitamento de janela e ignorava o orçamento de transbordo."""
    declarado = auto_compact_window(CLAUDE_CODE_WINDOW)
    topo_da_banda = declarado * 0.92  # extremo tardio da fração interna do cliente
    assert admission_ceiling(CLAUDE_CODE_WINDOW) - topo_da_banda >= CONTEXT_OVERSHOOT_MARGIN
    # e a compactação passa a cair em 63%-73% da janela real, por escolha
    assert 0.60 < declarado * 0.80 / CLAUDE_CODE_WINDOW < 0.70
    assert 0.70 < declarado * 0.92 / CLAUDE_CODE_WINDOW < 0.80


def test_teto_de_admissao_e_a_fronteira_exata_do_400():
    """Fixa a fronteira que o incidente cruzou por 411 tokens (0,3%), pra que
    qualquer mexida em CONTEXT_EXACT_SAFETY_FACTOR / CONTEXT_TEMPLATE_OVERHEAD /
    MIN_VIABLE_COMPLETION_TOKENS apareça aqui e não em produção."""
    teto = admission_ceiling(CLAUDE_CODE_WINDOW)
    assert teto == 128_054
    apply_context_budget(
        {"max_tokens": 8_000}, _machine(CLAUDE_CODE_WINDOW),
        est_tokens=teto, kind=EstimateKind.EXACT,
    )
    with pytest.raises(ContextWindowExceeded):
        apply_context_budget(
            {"max_tokens": 8_000}, _machine(CLAUDE_CODE_WINDOW),
            est_tokens=teto + 1, kind=EstimateKind.EXACT,
        )
    # o prompt do incidente, que motivou tudo isto
    assert 128_465 > teto


@pytest.mark.parametrize("window", [GO_WINDOW, 65_536, CLAUDE_CODE_WINDOW])
def test_janela_recomendada_deixa_a_saida_garantida(window):
    """Auto-consistência entre o valor que recomendamos ao cliente e o que o
    gateway aceita: um prompt exatamente do tamanho recomendado tem que passar
    E ainda deixar a saída mínima. Sem isto, mexer num fator de segurança
    quebra a recomendação sem nenhum teste reclamar."""
    budget = apply_context_budget(
        {"max_tokens": RESERVED_OUTPUT_TOKENS},
        _machine(window),
        est_tokens=auto_compact_window(window),
        kind=EstimateKind.EXACT,
    )
    assert budget.output_budget >= RESERVED_OUTPUT_TOKENS


def test_mensagem_de_erro_nao_manda_usar_compact():
    """/compact reenvia a transcrição INTEIRA, então é o pedido que dispara
    este erro — mandar o usuário usá-lo travava a sessão sem saída."""
    msg = context_exceeded_message(200_000, CLAUDE_CODE_WINDOW, EstimateKind.EXACT)
    assert "/compact" not in msg
    assert "/clear" in msg
    assert str(CLAUDE_CODE_WINDOW) in msg
    assert str(auto_compact_window(CLAUDE_CODE_WINDOW)) in msg
    assert "tokenizer" in msg
    assert "estimado" in context_exceeded_message(
        200_000, CLAUDE_CODE_WINDOW, EstimateKind.HEURISTIC
    )


def test_sem_max_model_len_o_budget_sai_sem_janela():
    budget = apply_context_budget({"max_tokens": 16_000}, {"id": "m1"}, est_tokens=999_999)
    assert budget.max_model_len is None and budget.output_budget is None
    assert budget.est_tokens == 999_999


def test_count_images_e_publico_e_conta_igual_a_estimativa():
    assert count_images(_messages_with_images(3)) == 3
    assert count_images(_messages_with_images(0)) == 0
    assert count_images("nao e lista") == 0


PRO_WINDOW = 65_536


def test_should_use_exact_token_count_longe_do_limite_fica_false():
    # prompt pequeno numa janela de 65536 — nem perto do threshold (0.7 default)
    assert should_use_exact_token_count(1_000, _machine(PRO_WINDOW)) is False


def test_should_use_exact_token_count_reproduz_incidente():
    # cenário real: heurística ~54730 contra janela de 65536 — é exatamente o
    # caso em que a heurística já rejeitaria e vale a pena confirmar com a
    # contagem real do tokenizer antes de decidir
    assert should_use_exact_token_count(54_730, _machine(PRO_WINDOW)) is True


def test_should_use_exact_token_count_sem_max_model_len_fica_false():
    for machine in ({"id": "m1"}, _machine(None), _machine(0)):
        assert should_use_exact_token_count(999_999, machine) is False


def test_shapes_de_erro():
    a = anthropic_error_body("msg")
    assert a["type"] == "error" and a["error"]["type"] == "invalid_request_error"
    o = openai_error_body("msg")
    assert o["error"]["code"] == "context_length_exceeded"


def test_shape_do_erro_vem_da_excecao_nao_da_url():
    """O 400 do incidente chegou ao Claude Code no shape OpenAI. O handler
    decidia pela URL, que é um proxy frágil do protocolo — quem sabe o que
    está atendendo é quem levanta o erro."""
    assert error_body_for("anthropic", "x")["type"] == "error"
    assert error_body_for("openai", "x")["error"]["code"] == "context_length_exceeded"
    # shape desconhecido cai no default do catch-all, não explode
    assert error_body_for("nao-existe", "x")["error"]["code"] == "context_length_exceeded"
    assert ContextWindowExceeded("x", shape="anthropic").shape == "anthropic"
    assert ContextWindowExceeded("x").shape == "openai"  # default preservado


def test_mensagem_de_erro_e_reconhecivel_pelo_cliente():
    """O Claude Code procura a frase canônica de estouro de contexto pra reagir
    sozinho. Uma mensagem só em português não casa com nada e a sessão morre
    sem chance de recuperação."""
    msg = context_exceeded_message(200_000, CLAUDE_CODE_WINDOW, EstimateKind.EXACT)
    assert msg.startswith("prompt is too long: 200000 tokens > 131072 maximum")
