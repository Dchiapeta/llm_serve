"""Testes de plan_limits.py — teto de janela de contexto por plano.

Rodar: pytest docker/gateway/test_plan_limits.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from plan_limits import capped_machine, context_cap, plan_window  # noqa: E402

GO_POD = {"id": "m-go", "max_model_len": 65536, "public_url": "https://go.test"}
PRO_POD = {"id": "m-pro", "max_model_len": 131072}


def test_teto_anunciado_no_site():
    assert context_cap("Go") == 32768
    assert context_cap("Pro") == 262144
    # Max e Enterprise são "personalizado": vale a janela da máquina
    assert context_cap("Max") is None
    assert context_cap("Enterprise") is None
    assert context_cap(None) is None


def test_plano_menor_que_a_maquina_limita_e_nao_muta_a_original():
    machine, capped = capped_machine(GO_POD, "Go")
    assert capped is True
    assert machine["max_model_len"] == 32768
    # o resto da máquina segue igual (public_url, id) — é ela que recebe o proxy
    assert machine["public_url"] == GO_POD["public_url"]
    assert GO_POD["max_model_len"] == 65536


def test_maquina_menor_que_o_plano_segue_a_maquina():
    # Pro: plano de 256K numa máquina de 128K — quem limita é a máquina, e o
    # estouro dela pode ir para o OpenRouter
    machine, capped = capped_machine(PRO_POD, "Pro")
    assert capped is False and machine is PRO_POD


def test_maquina_sem_janela_conhecida_recebe_o_teto():
    machine, capped = capped_machine({"id": "m"}, "Go")
    assert capped is True and machine["max_model_len"] == 32768


def test_plano_sem_teto_nao_mexe():
    machine, capped = capped_machine(GO_POD, "Max")
    assert capped is False and machine is GO_POD


def test_janela_para_o_openrouter():
    assert plan_window("Go") == {"max_model_len": 32768}
    assert plan_window("Max") == {}
