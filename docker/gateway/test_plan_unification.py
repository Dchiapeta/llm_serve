"""Go e Pro com as mesmas features (plan_unification.py, 15/10/2026).

    python3 -m pytest test_plan_unification.py

Com GO_PRO_UNIFIED desligada nada muda — os testes de cada módulo (cli_policy,
plan_limits, client_identity, document_extract, document_generate,
rate_limit) seguem cobrindo esse caminho. Aqui, com a flag ligada: o Go de LLM
lê os limites do Pro em todo lugar, e a stack Go de IMAGEM continua no Go.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import client_identity  # noqa: E402
import document_extract  # noqa: E402
import document_generate  # noqa: E402
import plan_limits  # noqa: E402
import plan_unification  # noqa: E402
from cli_policy import cli_block_reason  # noqa: E402
from plan_unification import feature_plan  # noqa: E402


@pytest.fixture
def unificado(monkeypatch):
    monkeypatch.setattr(plan_unification, "GO_PRO_UNIFIED", True)


def test_desligada_nao_muda_plano_nenhum():
    assert plan_unification.GO_PRO_UNIFIED is False  # default sem env
    for plano in ("Go", "VibeCoder", "Pro", "Max", None):
        assert feature_plan(plano) == plano


@pytest.mark.parametrize(
    "plano,categoria,esperado",
    [
        ("Go", "llm", "Pro"),
        ("Go", None, "Pro"),  # stack antiga sem categoria é LLM
        ("VibeCoder", "llm", "Pro"),
        ("Go", "image", "Go"),
        ("Pro", "llm", "Pro"),
        ("Max", "llm", "Max"),
        ("Image", "llm", "Image"),
        (None, "llm", None),
    ],
)
def test_so_o_go_de_llm_vira_pro(unificado, plano, categoria, esperado):
    assert feature_plan(plano, categoria) == esperado


def test_go_passa_a_codar(unificado):
    assert cli_block_reason("Go", "messages", None) is None
    assert cli_block_reason("Go", "chat/completions", "claude-cli/1.0") is None
    # imagem não é decisão comercial: continua barrada
    assert cli_block_reason("Image", "messages", None) is not None


def test_go_ganha_os_limites_do_pro(unificado):
    assert plan_limits.context_cap("Go") == plan_limits.context_cap("Pro")
    assert document_extract.limit_files("Go") == document_extract.limit_files("Pro")
    assert document_extract.limit_bytes("Go") == document_extract.limit_bytes("Pro")
    assert document_extract.limit_pages("Go") == document_extract.limit_pages("Pro")
    assert document_extract.limit_image_bytes("Go") == document_extract.limit_image_bytes("Pro")
    assert document_generate.limit_bytes("Go") == document_generate.limit_bytes("Pro")
    assert document_generate.limit_pages("Go") == document_generate.limit_pages("Pro")


def test_ambientes_respeitam_a_categoria(unificado):
    assert client_identity.client_cap("Go", "llm") == client_identity.client_cap("Pro")
    assert client_identity.client_cap("Go", "image") == client_identity.MAX_CLIENTS_BY_PLAN["Go"]


def test_rate_limit_do_go_vira_o_do_pro(unificado, monkeypatch):
    pytest.importorskip("fastapi", reason="importar main exige fastapi")
    monkeypatch.setenv("SUPABASE_URL", "https://exemplo.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")
    import main

    assert main.rate_limit_rpm("Go") == main.rate_limit_rpm("Pro")
    assert main.rate_limit_burst("Go") == main.rate_limit_burst("Pro")
