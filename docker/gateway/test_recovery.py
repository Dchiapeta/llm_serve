"""Testes de recovery.py — funções puras, sem fastapi/env/rede. Rodar de
docker/gateway/:

    python3 -m pytest test_recovery.py
"""

import time

from recovery import _NO_GPU_ERROR_PATTERNS, is_no_gpu_error, lock_active


# ---------- is_no_gpu_error ----------


def test_reconhece_a_mensagem_canonica_da_runpod():
    # o texto cru que o start_pod embute vindo da RunPod
    e = RuntimeError("RunPod POST /pods/x/start → 400: not enough free GPUs")
    assert is_no_gpu_error(e) is True


def test_reconhece_variacoes_de_caixa_e_wording():
    # o bug antigo: qualquer desvio do literal exato "not enough free GPUs"
    # caía em "failed" e a recriação nunca disparava
    for msg in (
        "Not Enough Free GPUs",
        "NOT ENOUGH FREE GPUS on host",
        "not enough gpu on this machine",
        "No GPUs available",
        "no gpu available right now",
        "insufficient GPU capacity",
    ):
        assert is_no_gpu_error(RuntimeError(msg)) is True, msg


def test_nao_classifica_falha_generica_como_no_gpu():
    # conservador de propósito: recriar DESTRÓI o pod, então erro genérico
    # NUNCA pode virar no_gpu
    for msg in (
        "RunPod POST /pods/x/start → 500: internal server error",
        "connection reset by peer",
        "pod not found",
        "unauthorized",
        "",
    ):
        assert is_no_gpu_error(RuntimeError(msg)) is False, msg


def test_aceita_qualquer_objeto_via_str():
    # a assinatura recebe `object` e normaliza via str()
    assert is_no_gpu_error("not enough free GPUs") is True
    assert is_no_gpu_error(123) is False


def test_padroes_todos_minusculos():
    # a normalização é lower() na entrada; os padrões precisam estar em minúsculas
    assert all(p == p.lower() for p in _NO_GPU_ERROR_PATTERNS)


# ---------- lock_active (trava com TTL) ----------


def test_chave_ausente_nao_esta_travada():
    assert lock_active({}, "m1", ttl=100) is False


def test_trava_recente_esta_ativa():
    lock = {"m1": time.time()}
    assert lock_active(lock, "m1", ttl=100) is True
    # continua registrada — ainda em andamento
    assert "m1" in lock


def test_trava_vazada_expira_e_libera():
    # entrada mais velha que o TTL = task morreu antes do finally → libera
    lock = {"m1": time.time() - 200}
    assert lock_active(lock, "m1", ttl=100) is False
    # e some do dict (auto-recuperação — o próximo request re-dispara)
    assert "m1" not in lock


def test_expiracao_no_limite_do_ttl():
    # idade >= ttl expira (comparação é `>=`)
    lock = {"m1": time.time() - 100}
    assert lock_active(lock, "m1", ttl=100) is False


def test_uma_chave_expirada_nao_afeta_outra_ativa():
    lock = {"velha": time.time() - 500, "nova": time.time()}
    assert lock_active(lock, "velha", ttl=180) is False
    assert lock_active(lock, "nova", ttl=180) is True
    assert "velha" not in lock
    assert "nova" in lock
