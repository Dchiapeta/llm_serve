"""Testes da identidade de ambiente (client_identity.py) — módulo puro.

    python3 -m pytest test_client_identity.py
"""

from client_identity import (
    MISSING_TOOL,
    UNKNOWN_TOOL,
    client_cap,
    client_fingerprint,
    client_ip,
    network_bucket,
    normalize_tool,
)


def _headers(**kwargs):
    """Headers case-insensitive como os do Starlette, em minúsculas."""
    return {k.replace("_", "-"): v for k, v in kwargs.items()}


# ---------- normalize_tool ----------


def test_reconhece_as_ferramentas_da_tabela():
    casos = {
        "claude-cli/2.1.4 (external, cli)": "claude-code",
        "codex_cli_rs/0.59.0": "codex",
        "Cursor/1.2 (openai-node/4.0)": "cursor",
        "Cline/3.1": "cline",
        "Roo-Code/1.0": "roo",
        "Continue/0.9": "continue",
        "openai-python/1.40.0": "sdk",
        "curl/8.4.0": "http",
    }
    for ua, slug in casos.items():
        assert normalize_tool(ua)[0] == slug, ua


def test_primeiro_match_vence():
    # UA com dois padrões: "cursor" é mais específico e vem antes de "openai-node"
    assert normalize_tool("Cursor/1.2 (openai-node/4.0)")[0] == "cursor"


def test_ua_desconhecido_e_ausente_sao_buckets_distintos():
    assert normalize_tool("MinhaFerramenta/9.9") == UNKNOWN_TOOL
    assert normalize_tool(None) == MISSING_TOOL
    assert normalize_tool("   ") == MISSING_TOOL


def test_versao_do_cliente_nao_cria_ferramenta_nova():
    # regressão do motivo de UNKNOWN_TOOL existir: atualizar o cliente não
    # pode transformar o mesmo lugar num ambiente novo
    a = normalize_tool("claude-cli/2.1.4 (external, cli)")
    b = normalize_tool("claude-cli/3.0.0 (external, cli)")
    assert a == b


# ---------- client_ip / network_bucket ----------


def test_cloudflare_tem_prioridade_sobre_forwarded_for():
    headers = _headers(
        cf_connecting_ip="203.0.113.7",
        x_forwarded_for="198.51.100.9, 10.0.0.1",
    )
    assert client_ip(headers) == "203.0.113.7"


def test_forwarded_for_usa_o_primeiro_da_cadeia():
    headers = _headers(x_forwarded_for="198.51.100.9, 10.0.0.1, 10.0.0.2")
    assert client_ip(headers) == "198.51.100.9"


def test_bucket_ipv4_e_barra_24():
    assert network_bucket(_headers(cf_connecting_ip="189.45.12.200")) == "189.45.12.0/24"


def test_bucket_ipv6_e_barra_48():
    assert (
        network_bucket(_headers(cf_connecting_ip="2001:db8:abcd:1234::1"))
        == "2001:db8:abcd::/48"
    )


def test_ip_com_porta_e_colchetes():
    assert network_bucket(_headers(x_forwarded_for="203.0.113.7:52310")) == "203.0.113.0/24"
    assert (
        network_bucket(_headers(x_forwarded_for="[2001:db8:abcd:1234::1]:443"))
        == "2001:db8:abcd::/48"
    )


def test_sem_header_ou_ip_invalido_vira_none():
    assert network_bucket(_headers()) is None
    assert network_bucket(_headers(cf_connecting_ip="nao-e-um-ip")) is None


# ---------- client_fingerprint ----------


def test_mesmo_ambiente_gera_o_mesmo_fingerprint():
    h = _headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.12.200")
    assert client_fingerprint(h)[0] == client_fingerprint(h)[0]


def test_troca_de_ultimo_octeto_nao_muda_o_ambiente():
    # é o motivo do /24: lease renovado pelo ISP não pode virar ambiente novo
    a = client_fingerprint(_headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.12.200"))
    b = client_fingerprint(_headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.12.7"))
    assert a[0] == b[0]


def test_troca_de_rede_muda_o_ambiente():
    a = client_fingerprint(_headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.12.200"))
    b = client_fingerprint(_headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.99.200"))
    assert a[0] != b[0]


def test_ferramentas_diferentes_na_mesma_rede_sao_ambientes_distintos():
    a = client_fingerprint(_headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.12.1"))
    b = client_fingerprint(_headers(user_agent="Cursor/1.2", cf_connecting_ip="189.45.12.1"))
    assert a[0] != b[0]


def test_fingerprint_devolve_rotulo_ua_cru_e_bucket():
    fp, label, ua, bucket = client_fingerprint(
        _headers(user_agent="claude-cli/2.1.4", cf_connecting_ip="189.45.12.200")
    )
    assert len(fp) == 64
    assert label == "Claude Code"
    assert ua == "claude-cli/2.1.4"
    assert bucket == "189.45.12.0/24"


def test_sem_rede_ainda_gera_fingerprint_estavel():
    a = client_fingerprint(_headers(user_agent="claude-cli/2.1.4"))
    b = client_fingerprint(_headers(user_agent="claude-cli/2.1.9"))
    assert a[0] == b[0] and a[3] is None


# ---------- client_cap ----------


def test_cap_por_plano():
    assert client_cap("Go") == 5
    assert client_cap("VibeCoder") == client_cap("Go")  # nome antigo, migration 0049
    assert client_cap("Pro") == 25
    assert client_cap("Max") == 50
    assert client_cap("Enterprise") is None


def test_plano_desconhecido_e_fail_open():
    assert client_cap("PlanoNovo") is None
    assert client_cap(None) is None
