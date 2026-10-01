"""Ligação da cota mensal de requisições no gateway (check_request_quota).

    python3 -m pytest test_request_quota_routes.py

request_quota.py testa a interpretação da RPC e o cache. Aqui testa-se o que
só existe em main.py: quem pula a consulta, a soma local entre leituras, a
falha aberta, o modo só-registro, e o 429 saindo no protocolo do cliente
ANTES de qualquer roteamento (resolve_route pode acordar máquina — uma stack
sem cota não pode custar GPU).

O Supabase fica de fora: `main.supa` é trocado por um duplo que conta as
chamadas da RPC.
"""

import asyncio
import logging
import os

import pytest

pytest.importorskip("fastapi", reason="importar main exige fastapi")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import request_quota  # noqa: E402

FIM = "2026-10-18T00:00:00+00:00"
GO = {"id": "stack-go", "plan": "Go", "category": "llm", "slug": "go-stack"}
IMAGEM = {"id": "stack-img", "plan": "Go", "category": "image", "slug": "img"}


class FakeSupa:
    def __init__(self, used=0, limit=4000, error: Exception | None = None):
        self.used, self.limit, self.error = used, limit, error
        self.calls = 0

    async def stack_request_quota(self, stack_id):
        self.calls += 1
        if self.error:
            raise self.error
        return [{
            "plan": "Go", "quota_limit": self.limit, "used": self.used,
            "cycle_start": "2026-09-18T00:00:00+00:00", "cycle_end": FIM,
        }]


@pytest.fixture(autouse=True)
def _estado_limpo(monkeypatch):
    main.request_quota_cache.clear()
    main.request_quota_warned.clear()
    main.rate_buckets.clear()
    monkeypatch.setattr(request_quota, "REQUEST_QUOTA_ENFORCE", True)
    yield
    main.request_quota_cache.clear()
    main.request_quota_warned.clear()
    main.rate_buckets.clear()


def _check(stack, purpose="customer"):
    return asyncio.run(main.check_request_quota(stack, (stack or {}).get("plan"), purpose))


# ---------- quem nem consulta ----------


@pytest.mark.parametrize(
    "stack,purpose",
    [(None, "customer"), (IMAGEM, "customer"), (GO, "playground")],
    ids=["sem-stack", "imagem", "playground"],
)
def test_casos_sem_cota_nao_gastam_rpc(monkeypatch, stack, purpose):
    fake = FakeSupa(used=10**6)
    monkeypatch.setattr(main, "supa", fake, raising=False)
    _check(stack, purpose)
    assert fake.calls == 0


def test_limite_nulo_libera(monkeypatch):
    fake = FakeSupa(used=10**6, limit=None)
    monkeypatch.setattr(main, "supa", fake, raising=False)
    _check(GO)
    _check(GO)
    assert fake.calls == 1  # o "sem teto" também fica no cache


# ---------- contagem ----------


def test_soma_local_corta_na_requisicao_exata(monkeypatch):
    fake = FakeSupa(used=3998)
    monkeypatch.setattr(main, "supa", fake, raising=False)
    _check(GO)  # 3999
    _check(GO)  # 4000
    with pytest.raises(main.RequestQuotaExceeded) as exc:
        _check(GO)
    assert exc.value.status_code == 429
    assert exc.value.headers["X-Quota-Used"] == "4000"
    assert fake.calls == 1  # uma leitura só; o resto foi soma local


def test_recusa_nao_soma(monkeypatch):
    fake = FakeSupa(used=4000)
    monkeypatch.setattr(main, "supa", fake, raising=False)
    for _ in range(3):
        with pytest.raises(main.RequestQuotaExceeded):
            _check(GO)
    assert main.request_quota_cache.get(GO["id"], main.time.time()).used == 4000


def test_recusa_e_http_exception(monkeypatch):
    # os `except HTTPException` dos call sites (release_flight etc.) seguem valendo
    monkeypatch.setattr(main, "supa", FakeSupa(used=4000), raising=False)
    with pytest.raises(HTTPException):
        _check(GO)


# ---------- falha aberta e modo só-registro ----------


def test_supabase_fora_libera_e_nao_martela(monkeypatch, caplog):
    fake = FakeSupa(error=httpx.ConnectError("fora"))
    monkeypatch.setattr(main, "supa", fake, raising=False)
    with caplog.at_level(logging.WARNING):
        _check(GO)
        _check(GO)
    assert fake.calls == 1
    assert "indisponível" in caplog.text


def test_resposta_que_nao_e_json_tambem_libera(monkeypatch):
    monkeypatch.setattr(main, "supa", FakeSupa(error=ValueError("Expecting value")), raising=False)
    _check(GO)


def test_modo_so_registro_nao_corta_e_avisa_uma_vez(monkeypatch, caplog):
    monkeypatch.setattr(request_quota, "REQUEST_QUOTA_ENFORCE", False)
    monkeypatch.setattr(main, "supa", FakeSupa(used=4000), raising=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            _check(GO)
    assert caplog.text.count("excederia") == 1


# ---------- wiring HTTP ----------


def _entry(stack):
    return {
        "account_id": "acc-1", "api_key_id": "key-1", "stack_id": stack["id"],
        "stacks": [stack], "purpose": "customer",
    }


@pytest.fixture
def cliente(monkeypatch):
    async def fake_authenticate(authorization, headers, path=None):
        return _entry(GO), "hash-1"

    async def nao_rotear(*a, **kw):
        raise AssertionError("cota esgotada não pode chegar ao roteamento")

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "resolve_route", nao_rotear)
    monkeypatch.setattr(main, "supa", FakeSupa(used=4000), raising=False)
    return TestClient(main.app, raise_server_exceptions=True)


def test_chat_completions_recebe_429_no_formato_openai(cliente):
    r = cliente.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x"},
        json={"model": "x", "messages": [{"role": "user", "content": "oi"}]},
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"
    assert "4000/4000" in r.json()["error"]["message"]
    assert r.headers["x-should-retry"] == "false"
    assert r.headers["x-quota-reset"] == FIM
    assert int(r.headers["retry-after"]) >= 1


def test_models_tambem_conta_e_e_cortado(cliente):
    r = cliente.get("/v1/models", headers={"Authorization": "Bearer sk-x"})
    assert r.status_code == 429


def test_messages_recebe_429_no_formato_anthropic(cliente):
    r = cliente.post(
        "/v1/messages",
        headers={"x-api-key": "sk-x"},
        json={"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "oi"}]},
    )
    assert r.status_code == 429
    body = r.json()
    assert body["type"] == "error" and body["error"]["type"] == "rate_limit_error"
    assert r.headers["x-should-retry"] == "false"


def test_documents_generate_html_tambem_conta(cliente):
    r = cliente.post(
        "/v1/documents/generate",
        headers={"Authorization": "Bearer sk-x"},
        json={"html": "<p>oi</p>"},
    )
    assert r.status_code == 429
