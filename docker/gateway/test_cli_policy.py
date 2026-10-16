"""Testes do corte de CLI por plano (cli_policy.py) — módulo puro.

    python3 -m pytest test_cli_policy.py

Desde 16/10/2026 todo plano de LLM tem CLI; só "Image" (legado da migration
0060) é barrado. Os testes de "camada" continuam existindo sobre o Image para
a mecânica path/User-Agent não ficar sem cobertura.
"""

import cli_policy
from cli_policy import CLI_BLOCKED_PLANS, CLI_ONLY_PATHS, CODING_TOOLS, cli_block_reason

LLM_PLANS = ("Go", "Pro", "Max", "Enterprise")

# ---------- planos de LLM: tudo liberado ----------


def test_so_image_e_barrado():
    assert CLI_BLOCKED_PLANS == {"Image"}


def test_rotas_de_cli_liberadas_em_todos_os_planos_de_llm():
    for plan in LLM_PLANS:
        for path in CLI_ONLY_PATHS:
            assert cli_block_reason(plan, path, None) is None, (plan, path)


def test_ferramentas_de_codigo_liberadas_em_todos_os_planos_de_llm():
    for plan in LLM_PLANS:
        for ua in ("claude-cli/2.1.4 (external, cli)", "Cursor/1.2", "codex_cli_rs/0.59.0"):
            assert cli_block_reason(plan, "chat/completions", ua) is None, (plan, ua)


# ---------- camada 1: path (a dura) ----------


def test_rotas_de_cli_bloqueadas_no_image():
    for path in CLI_ONLY_PATHS:
        assert cli_block_reason("Image", path, None), path


def test_path_bloqueia_mesmo_sem_user_agent():
    # é o que torna esta camada dura: não há header pra omitir ou forjar
    assert cli_block_reason("Image", "messages", None)
    assert cli_block_reason("Image", "messages", "curl/8.4.0")


# ---------- camada 2: user-agent (a best-effort) ----------


def test_ferramentas_de_codigo_bloqueadas_em_chat_completions():
    casos = {
        "cursor": "Cursor/1.2 (openai-node/4.0)",
        "cline": "Cline/3.1",
        "roo": "Roo-Code/1.0",
        "continue": "Continue/0.9",
        "claude-code": "claude-cli/2.1.4 (external, cli)",
        "codex": "codex_cli_rs/0.59.0",
    }
    # a lista do teste tem que cobrir CODING_TOOLS inteira, senão uma ferramenta
    # adicionada ao set passa a existir sem nenhum teste
    assert set(casos) == set(CODING_TOOLS)
    for slug, ua in casos.items():
        assert cli_block_reason("Image", "chat/completions", ua), slug


def test_sdk_e_http_passam_pela_camada_de_ua():
    for ua in (
        "openai-python/1.40.0",
        "openai-node/4.0",
        "anthropic-python/0.30",
        "langchain/0.2",
        "curl/8.4.0",
        "python-requests/2.32",
        "PostmanRuntime/7.37",
        # ferramenta com rótulo próprio, mas de automação: ganhar um slug em
        # TOOL_PATTERNS não pode virar corte de CLI
        "n8n",
    ):
        assert cli_block_reason("Image", "chat/completions", ua) is None, ua


def test_ua_ausente_ou_desconhecido_passa():
    # erra pra deixar passar: nenhum SDK legítimo com UA exótico leva 403
    assert cli_block_reason("Image", "chat/completions", None) is None
    assert cli_block_reason("Image", "chat/completions", "   ") is None
    assert cli_block_reason("Image", "chat/completions", "MinhaFerramenta/9.9") is None


def test_versao_do_cliente_nao_escapa_do_corte():
    for ua in ("claude-cli/2.1.4", "claude-cli/3.0.0", "ClaudeCode/9.9"):
        assert cli_block_reason("Image", "chat/completions", ua), ua


# ---------- fail-open ----------


def test_plano_desconhecido_ou_nulo_e_fail_open():
    # mesmo critério de usage_class.high_cap e client_identity.client_cap: plano
    # novo esquecido neste set libera CLI, nunca barra cliente pagante por omissão
    assert cli_block_reason("PlanoNovo", "messages", "claude-cli/2.1.4") is None
    assert cli_block_reason(None, "messages", "claude-cli/2.1.4") is None


def test_rotas_de_extracao_nunca_sao_cli():
    for path in ("documents/extract", "images/extract", "documents/generate", "embeddings"):
        assert cli_block_reason("Image", path, "openai-python/1.40.0") is None, path


# ---------- mensagem ----------


def test_mensagem_do_image_explica_o_produto_e_aponta_um_plano_de_llm():
    reason = cli_block_reason("Image", "messages", None)
    assert "Image" in reason and "/v1/images/" in reason
    assert "Go" in reason and "Pro" in reason


# ---------- contrato com client_identity ----------


def test_coding_tools_sao_slugs_reais_de_normalize_tool():
    """Um slug com typo aqui não levanta erro — só nunca casa, e o corte por UA
    silenciosamente deixa de existir para aquela ferramenta."""
    from client_identity import TOOL_PATTERNS

    conhecidos = {slug for _needles, slug, _label in TOOL_PATTERNS}
    assert CODING_TOOLS <= conhecidos


def test_enforce_liga_por_default():
    # a política sobe cortando; a env existe só como escape hatch
    assert cli_policy.CLI_POLICY_ENFORCE is True
