"""Wiring HTTP do repasse ao OpenRouter — o que openrouter.py sozinho não cobre.

    python3 -m pytest test_openrouter_routes.py

Aqui testa-se a LIGAÇÃO: qual request vai para o OpenRouter e qual segue para
máquina, o que sai no corpo repassado (modelo, system prompt da stack, teto de
tokens), o que volta ao cliente (sem o custo do fornecedor), o que é gravado em
gateway_requests e a trava das máquinas nas primitivas que ligam GPU.

authenticate/resolve_route reais ficam de fora (dependem de Supabase e de estado
de máquina). O resolve_route é um duplo que FALHA o teste se for chamado: toda
request repassada tem que chegar ao OpenRouter sem tocar em máquina nenhuma.
"""

import base64
import json
import os

import pytest

pytest.importorskip("fastapi", reason="wiring HTTP exige fastapi instalado")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

TEXT_SLUG = "anthropic/claude-sonnet-4.5"
IMAGE_SLUG = "google/gemini-2.5-flash-image"
PNG = b"\x89PNG\r\n\x1a\n" + b"conteudo da imagem"
PNG_B64 = base64.b64encode(PNG).decode()


def _entry(category="llm", system_prompt="Você é o assistente da Loja X."):
    return {
        "account_id": "acc-1",
        "api_key_id": "key-1",
        "stack_id": "stack-1",
        "purpose": "customer",
        "enable_knowledge_base": False,  # RAG desligado: nada de rede no teste
        "stacks": [{
            "id": "stack-1", "plan": "Go", "category": category,
            "system_prompt": system_prompt,
        }],
    }


class FakeSupa:
    def __init__(self, estado):
        self.estado = estado
        self.uploaded = {}
        self.rows = []

    async def get_setting(self, key, default):
        return self.estado["settings"].get(key, default)

    async def list_enabled_openrouter_models(self):
        return [{"slug": s, "kind": k} for s, k in self.estado["catalog"].items()]

    async def upload_image_object(self, storage_path, data, content_type):
        self.uploaded[storage_path] = data

    async def insert_image_generations(self, rows):
        self.rows.extend(rows)

    # chaves espelho (0072)
    async def get_openrouter_key(self, api_key_id):
        return self.estado["or_keys"].get(api_key_id)

    async def insert_openrouter_key(self, row):
        if row["api_key_id"] in self.estado["or_keys"]:
            return False
        self.estado["or_keys"][row["api_key_id"]] = row
        return True

    async def delete_openrouter_key(self, api_key_id):
        self.estado["or_keys"].pop(api_key_id, None)

    async def list_keys_without_openrouter_key(self):
        return [
            {"api_key_id": k, "account_id": "acc-1", "key_prefix": f"stac_{k}", "account_name": "Loja X"}
            for k in ("key-1", "key-2") if k not in self.estado["or_keys"]
        ]

    async def get_api_key_identity(self, api_key_id):
        if api_key_id != "key-1":
            return None
        return {"api_key_id": "key-1", "account_id": "acc-1",
                "key_prefix": "stac_ab", "account_name": "Loja X"}


@pytest.fixture
def rota(monkeypatch):
    estado = {
        "settings": {"machines_enabled": True, "openrouter_enabled": True},
        "catalog": {TEXT_SLUG: "text", IMAGE_SLUG: "image"},
        "entry": _entry(),
        "logged": [],
        "sent": [],
        "resposta": None,
        "or_keys": {},
    }
    supa = FakeSupa(estado)
    estado["supa"] = supa
    monkeypatch.setattr(main, "supa", supa, raising=False)

    async def fake_authenticate(authorization, headers, path):
        if not authorization:
            raise main.HTTPException(status_code=401, detail="sem chave")
        return estado["entry"], "hash"

    async def fake_authenticate_anthropic(authorization, x_api_key, headers, path):
        return estado["entry"], "hash", "Bearer sk-cliente"

    async def fake_resolve_route(account_id, entry):
        raise AssertionError("request repassada não pode resolver máquina")

    async def fake_quota(*a, **k):
        return None

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "authenticate_anthropic", fake_authenticate_anthropic)
    monkeypatch.setattr(main, "resolve_route", fake_resolve_route)
    monkeypatch.setattr(main, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(main, "check_image_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(main, "check_token_quota", fake_quota)
    monkeypatch.setattr(main, "log_gateway_request", lambda **kw: estado["logged"].append(kw))

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        estado["sent"].append((request, json.loads(body) if body else None))
        return estado["resposta"](request)

    monkeypatch.setattr(
        main, "openrouter_client",
        httpx.AsyncClient(
            base_url="https://openrouter.test/api/v1",
            headers={"Authorization": "Bearer sk-or-da-stac"},
            transport=httpx.MockTransport(handler),
        ),
    )
    main.settings_cache.clear()
    monkeypatch.setattr(main, "openrouter_catalog_cache", None)
    # chaves espelho desligadas por padrão; o fixture `espelho` liga
    monkeypatch.setattr(main, "openrouter_mgmt_client", None)
    monkeypatch.setattr(main, "openrouter_secret_box", None)
    main.openrouter_key_secrets.clear()
    main.openrouter_key_failures.clear()
    estado["client"] = TestClient(main.app)
    yield estado
    main.settings_cache.clear()


def _chat(estado, **body):
    return estado["client"].post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "oi"}], **body},
        headers={"Authorization": "Bearer sk-cliente"},
    )


# ---------------------------------------------------------------------------
# chat/completions
# ---------------------------------------------------------------------------


def test_chat_da_allowlist_vai_para_o_openrouter_sem_custo_na_resposta(rota):
    rota["resposta"] = lambda r: httpx.Response(200, json={
        "id": "gen-1", "model": TEXT_SLUG,
        "choices": [{"message": {"role": "assistant", "content": "olá"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14,
                  "cost": 0.00014, "is_byok": False},
    })
    r = _chat(rota, model=TEXT_SLUG, max_tokens=999999, n=3)
    assert r.status_code == 200
    assert "cost" not in r.json()["usage"] and "is_byok" not in r.json()["usage"]

    request, sent = rota["sent"][0]
    assert request.url.path == "/api/v1/chat/completions"
    # a chave do cliente NUNCA vai para o OpenRouter
    assert request.headers["authorization"] == "Bearer sk-or-da-stac"
    assert sent["model"] == TEXT_SLUG
    assert sent["max_tokens"] == main.OPENROUTER_MAX_TOKENS
    assert sent["n"] == 1
    # sem system do cliente: entra o da stack
    assert sent["messages"][0] == {"role": "system", "content": "Você é o assistente da Loja X."}

    log = rota["logged"][0]
    assert log["upstream"] == "openrouter" and log["machine_id"] is None
    assert log["model"] == TEXT_SLUG and log["cost_usd"] == 0.00014
    assert log["usage"]["prompt_tokens"] == 10


def test_chat_stream_repassa_e_tira_o_custo(rota):
    sse = (
        b": OPENROUTER PROCESSING\n\n"
        b'data: {"choices":[{"delta":{"content":"ol\xc3\xa1"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":2,"cost":0.3}}\n\n'
        b"data: [DONE]\n\n"
    )
    rota["resposta"] = lambda r: httpx.Response(
        200, content=sse, headers={"content-type": "text/event-stream"}
    )
    r = _chat(rota, model=TEXT_SLUG, stream=True)
    assert r.status_code == 200
    assert b'"cost"' not in r.content
    assert "olá".encode() in r.content and r.content.endswith(b"data: [DONE]\n\n")
    log = rota["logged"][0]
    assert log["stream"] is True and log["status_code"] == 200
    assert log["cost_usd"] == 0.3 and log["usage"]["completion_tokens"] == 2


def test_erro_no_meio_do_stream_nao_vira_sucesso_no_log(rota):
    sse = (
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        b'data: {"error":{"code":502,"message":"Provider down"},'
        b'"choices":[{"delta":{"content":""},"finish_reason":"error"}]}\n\n'
    )
    rota["resposta"] = lambda r: httpx.Response(
        200, content=sse, headers={"content-type": "text/event-stream"}
    )
    r = _chat(rota, model=TEXT_SLUG, stream=True)
    assert r.status_code == 200  # o cabeçalho já tinha ido
    assert rota["logged"][0]["status_code"] == 502


def test_system_do_cliente_ganha_do_da_stack(rota):
    rota["resposta"] = lambda r: httpx.Response(200, json={"choices": []})
    rota["client"].post(
        "/v1/chat/completions",
        json={"model": TEXT_SLUG, "messages": [
            {"role": "system", "content": "sou o Cursor"},
            {"role": "user", "content": "oi"},
        ]},
        headers={"Authorization": "Bearer sk-cliente"},
    )
    _req, sent = rota["sent"][0]
    assert sent["messages"][0] == {"role": "system", "content": "sou o Cursor"}


@pytest.mark.parametrize("status", [401, 402])
def test_problema_da_conta_da_stac_vira_503_generico(rota, status):
    rota["resposta"] = lambda r: httpx.Response(status, json={
        "error": {"code": status, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"},
    })
    r = _chat(rota, model=TEXT_SLUG)
    assert r.status_code == 503
    assert "openrouter" not in r.text.lower()
    assert rota["logged"][0]["status_code"] == status


def test_erro_da_request_do_cliente_passa_com_o_status(rota):
    rota["resposta"] = lambda r: httpx.Response(400, json={
        "error": {"code": 400, "message": "context length exceeded"},
    })
    r = _chat(rota, model=TEXT_SLUG)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "context length exceeded"


def test_200_so_com_error_vira_502(rota):
    rota["resposta"] = lambda r: httpx.Response(200, json={
        "error": {"code": 502, "message": "Provider returned error"},
    })
    r = _chat(rota, model=TEXT_SLUG)
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# destino: allowlist x máquinas
# ---------------------------------------------------------------------------


def test_modelo_fora_da_lista_com_maquinas_desligadas_lista_os_aceitos(rota):
    rota["settings"]["machines_enabled"] = False
    r = _chat(rota, model="gpt-5")
    assert r.status_code == 404
    assert TEXT_SLUG in r.json()["detail"]
    assert IMAGE_SLUG not in r.json()["detail"]  # modelo de imagem não atende chat
    assert rota["sent"] == []


def test_tudo_desligado_e_503(rota):
    rota["settings"] = {"machines_enabled": False, "openrouter_enabled": False}
    r = _chat(rota, model=TEXT_SLUG)
    assert r.status_code == 503
    assert rota["sent"] == []


def test_repasse_desligado_modelo_da_lista_vai_para_maquina(rota, monkeypatch):
    rota["settings"]["openrouter_enabled"] = False
    chamado = []

    async def resolve_route(account_id, entry):
        chamado.append(True)
        raise main.HTTPException(status_code=503, detail="máquina de teste")

    monkeypatch.setattr(main, "resolve_route", resolve_route)
    r = _chat(rota, model=TEXT_SLUG)
    assert chamado and r.status_code == 503
    assert rota["sent"] == []


def test_models_sem_maquinas_lista_so_a_allowlist(rota):
    rota["settings"]["machines_enabled"] = False
    r = rota["client"].get("/v1/models", headers={"Authorization": "Bearer sk-cliente"})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == [TEXT_SLUG]


# ---------------------------------------------------------------------------
# /v1/messages (Anthropic)
# ---------------------------------------------------------------------------


def test_messages_repassa_no_formato_anthropic(rota):
    rota["resposta"] = lambda r: httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "oi"}],
        "usage": {"input_tokens": 12, "output_tokens": 18, "cost": 0.01},
    })
    system_blocks = [{"type": "text", "text": "Claude Code", "cache_control": {"type": "ephemeral"}}]
    r = rota["client"].post(
        "/v1/messages",
        json={"model": TEXT_SLUG, "max_tokens": 64000, "system": system_blocks,
              "thinking": {"type": "enabled", "budget_tokens": 40000},
              "messages": [{"role": "user", "content": "oi"}]},
        headers={"x-api-key": "sk-cliente"},
    )
    assert r.status_code == 200 and "cost" not in r.json()["usage"]
    request, sent = rota["sent"][0]
    assert request.url.path == "/api/v1/messages"
    # system do cliente intacto, com o cache_control
    assert sent["system"] == system_blocks
    assert sent["max_tokens"] == main.OPENROUTER_MAX_TOKENS
    assert sent["thinking"]["budget_tokens"] < sent["max_tokens"]
    log = rota["logged"][0]
    assert log["path"] == "messages" and log["cost_usd"] == 0.01
    assert log["usage"]["prompt_tokens"] == 12


def test_messages_sem_system_recebe_o_da_stack(rota):
    rota["resposta"] = lambda r: httpx.Response(200, json={"content": []})
    rota["client"].post(
        "/v1/messages",
        json={"model": TEXT_SLUG, "max_tokens": 100,
              "messages": [{"role": "user", "content": "oi"}]},
        headers={"x-api-key": "sk-cliente"},
    )
    _req, sent = rota["sent"][0]
    assert sent["system"] == "Você é o assistente da Loja X."


def test_messages_fora_da_lista_sem_maquinas(rota):
    rota["settings"]["machines_enabled"] = False
    r = rota["client"].post(
        "/v1/messages",
        json={"model": "claude-opus-5", "max_tokens": 100, "messages": []},
        headers={"x-api-key": "sk-cliente"},
    )
    assert r.status_code == 404 and TEXT_SLUG in r.json()["detail"]


# ---------------------------------------------------------------------------
# imagens
# ---------------------------------------------------------------------------


def _image_response(r):
    return httpx.Response(200, json={
        "created": 1,
        "data": [{"b64_json": PNG_B64, "media_type": "image/png"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 4175, "cost": 0.04},
    })


def test_generations_vai_para_o_images_do_openrouter(rota):
    rota["entry"] = _entry(category="image", system_prompt=None)
    rota["resposta"] = _image_response
    r = rota["client"].post(
        "/v1/images/generations",
        json={"model": IMAGE_SLUG, "prompt": "um gato", "size": "1024x1024",
              "steps": 8, "n": 20},
        headers={"Authorization": "Bearer sk-cliente"},
    )
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"] == PNG_B64
    assert "cost" not in r.json()["usage"]
    assert "X-Stac-Image-Batch" in r.headers
    request, sent = rota["sent"][0]
    assert request.url.path == "/api/v1/images"
    assert sent == {"model": IMAGE_SLUG, "prompt": "um gato", "size": "1024x1024",
                    "n": main.OPENROUTER_MAX_IMAGES}
    # guardada no bucket antes do 200, como no pod
    assert list(rota["supa"].uploaded.values()) == [PNG]
    assert rota["supa"].rows[0]["machine_id"] is None
    assert rota["logged"][0]["cost_usd"] == 0.04


def test_edits_manda_as_referencias_como_data_url(rota):
    rota["entry"] = _entry(category="image", system_prompt=None)
    rota["resposta"] = _image_response
    r = rota["client"].post(
        "/v1/images/edits",
        data={"model": IMAGE_SLUG, "prompt": "troca o fundo", "n": "1"},
        files=[("image[]", ("foto.png", PNG, "image/png"))],
        headers={"Authorization": "Bearer sk-cliente"},
    )
    assert r.status_code == 200
    _req, sent = rota["sent"][0]
    assert sent["prompt"] == "troca o fundo" and sent["n"] == 1
    ref = sent["input_references"][0]["image_url"]["url"]
    assert ref == "data:image/png;base64," + PNG_B64


def test_edits_sem_referencia_e_400(rota):
    rota["entry"] = _entry(category="image", system_prompt=None)
    r = rota["client"].post(
        "/v1/images/edits",
        data={"model": IMAGE_SLUG, "prompt": "x"},
        files=[("outro", ("a.txt", b"x", "text/plain"))],
        headers={"Authorization": "Bearer sk-cliente"},
    )
    assert r.status_code == 400 and rota["sent"] == []


# ---------------------------------------------------------------------------
# trava das máquinas nas primitivas que ligam GPU
# ---------------------------------------------------------------------------


@pytest.fixture
def maquinas_desligadas(monkeypatch):
    async def desligado():
        return False

    decisoes = []
    monkeypatch.setattr(main, "machines_enabled", desligado)
    monkeypatch.setattr(main, "_record_decision", lambda outcome, cause, trig: decisoes.append(cause))
    return decisoes


def test_wake_negado_com_maquinas_desligadas(maquinas_desligadas, monkeypatch):
    import asyncio

    monkeypatch.setattr(main, "runpod_client", object(), raising=False)
    machine = {"id": "m-1", "name": "m", "runpod_pod_id": "pod-1"}
    assert asyncio.run(main.wake_machine(machine, "teste", cause="wake.request.stack_home_paused")) == "failed"
    assert maquinas_desligadas == ["wake_denied.machines_disabled"]


def test_provisionamento_de_request_negado_com_maquinas_desligadas(maquinas_desligadas):
    import asyncio

    # ignore_switch=True é o caminho de REQUEST, que pula o auto_provision_enabled
    # mas não pode pular este interruptor
    assert asyncio.run(main.try_provision_for_request(
        "Go", "teste", "llm", cause="provision.request.no_base_machine"
    )) is False
    assert maquinas_desligadas == ["provision_denied.machines_disabled"]


def test_recriacao_negada_com_maquinas_desligadas(maquinas_desligadas):
    import asyncio

    machine = {"id": "m-2", "name": "m"}
    assert asyncio.run(main.try_recreate_machine(machine, "teste")) is False
    assert maquinas_desligadas == ["recreate_denied.machines_disabled"]


# ---------------------------------------------------------------------------
# chave espelho no OpenRouter por chave da Stac (0072)
# ---------------------------------------------------------------------------


@pytest.fixture
def espelho(rota, monkeypatch):
    from cryptography.fernet import Fernet

    import openrouter_keys

    box = openrouter_keys.SecretBox(Fernet.generate_key().decode())
    mgmt = {"criadas": [], "apagadas": [], "falhar": False, "n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-or-mgmt"
        if request.method == "DELETE":
            mgmt["apagadas"].append(request.url.path)
            return httpx.Response(200, json={"deleted": True})
        if mgmt["falhar"]:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        mgmt["n"] += 1
        body = json.loads(await request.aread())
        mgmt["criadas"].append(body["name"])
        return httpx.Response(201, json={
            "data": {"hash": f"hash-{mgmt['n']}", "name": body["name"]},
            "key": f"sk-or-v1-espelho-{mgmt['n']}",
        })

    monkeypatch.setattr(main, "openrouter_secret_box", box)
    monkeypatch.setattr(main, "openrouter_mgmt_client", httpx.AsyncClient(
        base_url="https://openrouter.test/api/v1",
        headers={"Authorization": "Bearer sk-or-mgmt"},
        transport=httpx.MockTransport(handler),
    ))
    rota["entry"] = {**rota["entry"], "account_name": "Loja X", "key_prefix": "stac_ab"}
    rota["resposta"] = lambda r: httpx.Response(200, json={"choices": [], "usage": {
        "prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}})
    rota["mgmt"] = mgmt
    rota["box"] = box
    return rota


def test_primeira_request_cria_a_espelho_e_as_seguintes_reusam(espelho):
    assert _chat(espelho, model=TEXT_SLUG).status_code == 200
    assert _chat(espelho, model=TEXT_SLUG).status_code == 200
    assert espelho["mgmt"]["criadas"] == ["Stac · Loja X · stac_ab · key-1"]
    for request, _body in espelho["sent"]:
        assert request.headers["authorization"] == "Bearer sk-or-v1-espelho-1"
    row = espelho["or_keys"]["key-1"]
    assert row["openrouter_hash"] == "hash-1"
    # o segredo vai cifrado para o banco
    assert "espelho" not in row["secret_encrypted"]
    assert espelho["box"].decrypt(row["secret_encrypted"]) == "sk-or-v1-espelho-1"


def test_espelho_ja_gravada_e_lida_do_banco(espelho):
    espelho["or_keys"]["key-1"] = {
        "openrouter_hash": "hash-antiga",
        "secret_encrypted": espelho["box"].encrypt("sk-or-v1-antiga"),
    }
    _chat(espelho, model=TEXT_SLUG)
    assert espelho["mgmt"]["criadas"] == []
    assert espelho["sent"][0][0].headers["authorization"] == "Bearer sk-or-v1-antiga"


def test_falha_ao_criar_usa_a_compartilhada_e_nao_martela(espelho):
    espelho["mgmt"]["falhar"] = True
    assert _chat(espelho, model=TEXT_SLUG).status_code == 200
    assert _chat(espelho, model=TEXT_SLUG).status_code == 200
    for request, _body in espelho["sent"]:
        assert request.headers["authorization"] == "Bearer sk-or-da-stac"
    # a segunda request caiu no cooldown, sem nova tentativa
    assert espelho["mgmt"]["n"] == 0 and "key-1" in main.openrouter_key_failures


def test_401_na_espelho_esquece_para_recriar(espelho):
    _chat(espelho, model=TEXT_SLUG)
    espelho["resposta"] = lambda r: httpx.Response(401, json={"error": {"message": "User not found"}})
    r = _chat(espelho, model=TEXT_SLUG)
    assert r.status_code == 503
    assert "key-1" not in main.openrouter_key_secrets
    assert "key-1" not in espelho["or_keys"]


def test_admin_provision_cria_a_espelho_da_chave_nova(espelho, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda secret: None)
    r = espelho["client"].post("/admin/openrouter-keys/provision", json={"api_key_id": "key-1"})
    assert r.json() == {"ok": True}
    assert espelho["mgmt"]["criadas"] == ["Stac · Loja X · stac_ab · key-1"]
    r = espelho["client"].post("/admin/openrouter-keys/provision", json={"api_key_id": "outra"})
    assert r.status_code == 404


def test_provision_nao_cria_com_o_repasse_desligado(espelho, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda secret: None)
    espelho["settings"]["openrouter_enabled"] = False
    r = espelho["client"].post("/admin/openrouter-keys/provision", json={"api_key_id": "key-1"})
    assert r.json() == {"ok": False, "reason": "repasse ao OpenRouter desligado na página Modelos"}
    assert espelho["mgmt"]["criadas"] == []


def test_provision_diz_qual_variavel_falta(rota, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda secret: None)
    monkeypatch.setattr(
        main, "openrouter_keys_disabled_reason",
        "variável ausente no processo do gateway: OPENROUTER_MANAGEMENT_KEY",
    )
    r = rota["client"].post("/admin/openrouter-keys/provision", json={"api_key_id": "key-1"})
    assert r.json()["reason"].endswith("OPENROUTER_MANAGEMENT_KEY")


def test_backfill_cria_so_as_que_faltam(espelho, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda secret: None)
    espelho["or_keys"]["key-1"] = {
        "openrouter_hash": "h", "secret_encrypted": espelho["box"].encrypt("sk-or-v1-x"),
    }

    async def sem_espera(_):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", sem_espera)
    r = espelho["client"].post("/admin/openrouter-keys/backfill")
    assert r.json() == {"ok": True, "queued": 1}
    assert espelho["mgmt"]["criadas"] == ["Stac · Loja X · stac_key-2 · key-2"]
