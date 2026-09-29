"""Testes de openrouter_keys.py — nome, parse da criação e cifra das chaves
espelho do OpenRouter.

Rodar: pytest docker/gateway/test_openrouter_keys.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("cryptography", reason="cifra exige cryptography instalado")

from cryptography.fernet import Fernet  # noqa: E402

from openrouter_keys import SecretBox, key_name, parse_created  # noqa: E402


def test_key_name_com_conta_prefixo_e_id():
    entry = {"account_name": "Loja X", "key_prefix": "stac_ab12",
             "api_key_id": "0f1e2d3c-aaaa-bbbb-cccc-000000000000"}
    assert key_name(entry) == "Stac · Loja X · stac_ab12 · 0f1e2d3c"


def test_key_name_sem_nome_de_conta_e_truncado():
    assert key_name({"account_id": "acc-1"}) == "Stac · acc-1"
    assert len(key_name({"account_name": "x" * 500})) == 120


def test_parse_created_formato_documentado():
    payload = {"data": {"hash": "f01d", "name": "n"}, "key": "sk-or-v1-abc"}
    assert parse_created(payload) == ("f01d", "sk-or-v1-abc")


@pytest.mark.parametrize("payload", [
    None, {}, {"key": "sk"}, {"data": {"hash": "h"}}, {"data": {"hash": ""}, "key": "sk"},
])
def test_parse_created_recusa_resposta_incompleta(payload):
    with pytest.raises(ValueError):
        parse_created(payload)


def test_secret_box_ida_e_volta():
    box = SecretBox(Fernet.generate_key().decode())
    token = box.encrypt("sk-or-v1-segredo")
    assert "segredo" not in token
    assert box.decrypt(token) == "sk-or-v1-segredo"


def test_secret_box_chave_errada():
    token = SecretBox(Fernet.generate_key().decode()).encrypt("sk")
    with pytest.raises(ValueError):
        SecretBox(Fernet.generate_key().decode()).decrypt(token)
    with pytest.raises(ValueError):
        SecretBox("nao-e-uma-chave-fernet")
