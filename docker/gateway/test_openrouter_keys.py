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


def test_key_name_e_conta_barra_chave():
    entry = {"account_id": "8a1b2c3d-0000-0000-0000-000000000001", "account_name": "Loja X",
             "key_prefix": "stac_ab12", "api_key_id": "0f1e2d3c-aaaa-bbbb-cccc-000000000000"}
    assert key_name(entry) == (
        "8a1b2c3d-0000-0000-0000-000000000001/0f1e2d3c-aaaa-bbbb-cccc-000000000000"
    )


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


def test_secret_box_erro_diz_o_tamanho_sem_o_valor():
    valor = Fernet.generate_key().decode()[:30]  # colado pela metade
    with pytest.raises(ValueError) as exc:
        SecretBox(valor)
    msg = str(exc.value)
    assert "30 caracteres" in msg
    assert valor not in msg
