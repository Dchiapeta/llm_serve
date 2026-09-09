"""Token bucket do gateway: taxa, burst e o que compartilha o teto.

    python3 -m pytest test_rate_limit.py

Duas propriedades que estes testes protegem, e que são fáceis de quebrar sem
perceber porque ninguém vê o bucket:

  * Go/llm e Go/image são produtos diferentes. O primeiro mantém o limite de
    chat; o segundo tem 10 submissões/min e rajada inicial 3.
  * a chave do bucket é QUEM divide o teto. Nas rotas de imagem é a stack; se
    voltar a ser a chave, o teto passa a ser multiplicado por quantas chaves a
    stack emitir, sem que nada falhe visivelmente.
"""

import os

import pytest

pytest.importorskip("fastapi", reason="importar main exige fastapi")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402


@pytest.fixture(autouse=True)
def _bucket_limpo():
    main.rate_buckets.clear()
    yield
    main.rate_buckets.clear()


def _gastar(bucket_key, plan, n, *, image=False):
    """Consome n permissões; devolve quantas passaram antes do primeiro 429."""
    for i in range(n):
        try:
            checker = main.check_image_rate_limit if image else main.check_rate_limit
            checker(bucket_key, plan)
        except HTTPException:
            return i
    return n


# ---------- taxa e burst são independentes ----------


def test_go_image_tem_taxa_comercial_de_10_por_minuto():
    assert main.image_rate_limit_rpm("Go") == 10.0


def test_go_image_tem_burst_de_3():
    # alinhado à QUEUE_CAPACITY para a rajada inicial. A concorrência contínua
    # é protegida separadamente por check_concurrency.
    assert main.image_rate_limit_burst("Go") == 3.0


def test_go_llm_e_go_image_tem_limites_independentes():
    assert main.rate_limit_rpm("Go") == 60.0
    assert main.image_rate_limit_rpm("Go") == 10.0
    assert main.rate_limit_burst("Go") == 60.0
    assert main.image_rate_limit_burst("Go") == 3.0


@pytest.mark.parametrize("plano", ["Go", "Pro", "Max", "Enterprise", "VibeCoder"])
def test_planos_de_llm_mantem_burst_igual_a_taxa(plano):
    # RATE_LIMIT_BURST é opt-in: quem não está lá tem o comportamento de sempre
    assert main.rate_limit_burst(plano) == main.rate_limit_rpm(plano)


def test_plano_desconhecido_cai_no_default_nas_duas_grandezas():
    assert main.rate_limit_rpm("Inexistente") == main.DEFAULT_RATE_LIMIT_RPM
    assert main.rate_limit_burst("Inexistente") == main.DEFAULT_RATE_LIMIT_RPM


def test_plano_none_nao_estoura():
    assert main.rate_limit_rpm(None) == main.DEFAULT_RATE_LIMIT_RPM
    assert main.rate_limit_burst(None) == main.DEFAULT_RATE_LIMIT_RPM


# ---------- o bucket respeita o burst, não a taxa ----------


def test_image_aceita_exatamente_3_de_uma_vez():
    assert _gastar("stack:s1", "Go", 10, image=True) == 3


def test_a_quarta_requisicao_instantanea_do_image_e_429():
    _gastar("stack:s1", "Go", 3, image=True)
    with pytest.raises(HTTPException) as e:
        main.check_image_rate_limit("stack:s1", "Go")
    assert e.value.status_code == 429


def test_plano_de_llm_continua_aceitando_a_rajada_inteira():
    # a mudança do Image não pode ter encolhido o burst de quem já existia
    assert _gastar("key-hash", "Pro", 130) == 120


# ---------- Retry-After ----------


def test_retry_after_vem_no_429():
    _gastar("stack:s1", "Go", 3, image=True)
    with pytest.raises(HTTPException) as e:
        main.check_image_rate_limit("stack:s1", "Go")
    assert int(e.value.headers["Retry-After"]) >= 1


def test_retry_after_do_image_reflete_a_taxa_e_nao_o_burst():
    """10/min = um token a cada 6s; Retry-After usa a taxa, não o burst 3."""
    _gastar("stack:s1", "Go", 3, image=True)
    with pytest.raises(HTTPException) as e:
        main.check_image_rate_limit("stack:s1", "Go")
    # 60/10 = 6s, +1 do arredondamento defensivo
    assert int(e.value.headers["Retry-After"]) <= 7


# ---------- quem divide o teto ----------


def test_buckets_diferentes_nao_se_afetam():
    _gastar("stack:s1", "Go", 3, image=True)
    assert _gastar("stack:s2", "Go", 3, image=True) == 3


def test_chave_de_bucket_da_stack_tem_prefixo():
    # `stack:` nunca colide com um key_hash, que é hex de sha256 e não tem ':'
    chave = main.rate_bucket_for_stack("abc-123")
    assert chave == "stack:abc-123"
    assert ":" not in "0123456789abcdef" * 4


def test_duas_chaves_da_mesma_stack_dividem_o_mesmo_bucket():
    """O ponto de rate_bucket_for_stack existir.

    Duas chaves da mesma stack somam no mesmo teto; se cada uma tivesse o seu,
    emitir chaves multiplicaria a capacidade que foi dimensionada pela GPU."""
    bucket = main.rate_bucket_for_stack("s1")
    gastas_pela_primeira = _gastar(bucket, "Go", 2, image=True)
    gastas_pela_segunda = _gastar(bucket, "Go", 10, image=True)
    assert gastas_pela_primeira == 2
    assert gastas_pela_segunda == 1  # sobrou 1 do burst de 3, não outro bucket


# ---------- reposição ----------


def test_bucket_repoe_com_o_tempo(monkeypatch):
    relogio = {"t": 1000.0}
    monkeypatch.setattr(main.time, "time", lambda: relogio["t"])

    assert _gastar("stack:s1", "Go", 3, image=True) == 3
    relogio["t"] += 12.0  # 12s a 10/min = 2 tokens
    assert _gastar("stack:s1", "Go", 5, image=True) == 2


def test_reposicao_nao_passa_do_burst(monkeypatch):
    relogio = {"t": 1000.0}
    monkeypatch.setattr(main.time, "time", lambda: relogio["t"])

    _gastar("stack:s1", "Go", 3, image=True)
    relogio["t"] += 3600.0  # uma hora parado
    # o bucket enche até o teto e para: não acumula uma hora de crédito
    assert _gastar("stack:s1", "Go", 20, image=True) == 3
