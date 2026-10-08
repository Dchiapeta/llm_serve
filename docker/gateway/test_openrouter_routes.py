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


def _entry(category="llm", system_prompt="Você é o assistente da Loja X.", plan="Go"):
    return {
        "account_id": "acc-1",
        "api_key_id": "key-1",
        "stack_id": "stack-1",
        "purpose": "customer",
        "enable_knowledge_base": False,  # RAG desligado: nada de rede no teste
        "stacks": [{
            "id": "stack-1", "plan": plan, "category": category,
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
        rows = []
        for s, k in self.estado["catalog"].items():
            row = {"slug": s, "kind": k, "fallback": s == self.estado["fallback"]}
            # sem "plans" no estado: linha de antes da 0074 (aberta a todos)
            if self.estado.get("plans") is not None:
                row["plans"] = self.estado["plans"].get(s, [])
                row["fallback_plans"] = self.estado["fallback_plans"].get(s, [])
            rows.append(row)
        return rows

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
        "fallback": None,
        "machine": None,
        "plan": "Max",
        "resolve_calls": [],
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
        # default: nenhuma máquina disponível (o real dispara o wake e dá 503)
        estado["resolve_calls"].append(entry["stack_id"])
        machine = estado["machine"]
        if machine is None:
            raise main.HTTPException(status_code=503, detail="máquina ligando, tente novamente")
        # plano fora de REASONING_LEAK_PLANS: o filtro de <think> do Qwen não
        # é o assunto destes testes
        return machine, False, estado["plan"], "stack-1"

    async def fake_quota(*a, **k):
        return None

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "authenticate_anthropic", fake_authenticate_anthropic)
    monkeypatch.setattr(main, "resolve_route", fake_resolve_route)
    monkeypatch.setattr(main, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(main, "check_image_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(main, "check_token_quota", fake_quota)
    monkeypatch.setattr(main, "check_request_quota", fake_quota)
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
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-or-da-stac")
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
    # imagem fica FORA da regra de reserva: modelo da lista nem consulta máquina
    assert rota["resolve_calls"] == []


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
    assert espelho["mgmt"]["criadas"] == ["acc-1/key-1"]
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
    assert espelho["mgmt"]["criadas"] == ["acc-1/key-1"]
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
    assert espelho["mgmt"]["criadas"] == ["acc-1/key-2"]


def test_sem_compartilhada_usa_so_a_espelho(espelho, monkeypatch):
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")
    assert _chat(espelho, model=TEXT_SLUG).status_code == 200
    assert espelho["sent"][0][0].headers["authorization"] == "Bearer sk-or-v1-espelho-1"


def test_sem_compartilhada_e_sem_espelho_e_503(espelho, monkeypatch):
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")
    espelho["mgmt"]["falhar"] = True
    r = _chat(espelho, model=TEXT_SLUG)
    assert r.status_code == 503
    assert espelho["sent"] == []
    assert espelho["logged"][0]["status_code"] == 503


# ---------------------------------------------------------------------------
# OpenRouter como reserva das máquinas (30/09, migration 0073)
# ---------------------------------------------------------------------------

QWEN = "qwen/qwen3.8-27b"
POD = {
    "id": "m-pro", "public_url": "https://pod.test", "served_model_name": "pro-base",
    "model_name": "Qwen/Qwen3.8-27B", "max_model_len": 131072, "max_concurrent_seqs": 16,
}


@pytest.fixture
def reserva(rota, monkeypatch):
    rota["catalog"][QWEN] = "text"
    rota["fallback"] = QWEN
    rota["resposta"] = lambda r: httpx.Response(200, json={"choices": [], "usage": {
        "prompt_tokens": 1, "completion_tokens": 1}})
    rota["pod"] = []

    async def pod_handler(request: httpx.Request) -> httpx.Response:
        rota["pod"].append(json.loads(await request.aread()))
        return rota["pod_resposta"](request)

    async def no_touch(*a, **k):
        return None

    monkeypatch.setattr(main, "maybe_touch", no_touch)
    monkeypatch.setattr(main, "proxy_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(pod_handler)),
                        raising=False)
    main.in_flight.clear()
    yield rota
    assert sum(main.in_flight.values()) == 0  # nenhum desvio vaza vaga
    main.in_flight.clear()


def test_sem_maquina_modelo_da_lista_responde_pelo_openrouter(reserva):
    r = _chat(reserva, model=TEXT_SLUG)
    assert r.status_code == 200
    assert reserva["resolve_calls"] == ["stack-1"]  # a máquina foi pedida (wake)
    assert reserva["sent"][0][1]["model"] == TEXT_SLUG


def test_sem_maquina_pro_base_cai_no_reserva(reserva):
    _chat(reserva, model="pro-base")
    assert reserva["sent"][0][1]["model"] == QWEN


def test_maquina_disponivel_responde_qualquer_modelo_com_o_nome_pedido(reserva):
    reserva["machine"] = POD
    reserva["pod_resposta"] = lambda r: httpx.Response(200, json={
        "id": "x", "model": "pro-base",
        "choices": [{"message": {"role": "assistant", "content": "olá"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })
    r = _chat(reserva, model=TEXT_SLUG)
    assert r.status_code == 200
    assert reserva["sent"] == []  # nada foi pro OpenRouter
    assert reserva["pod"][0]["model"] == "pro-base"  # a máquina recebe o nome dela
    assert r.json()["model"] == TEXT_SLUG  # o cliente vê o que pediu
    assert r.json()["choices"][0]["message"]["content"] == "olá"


def test_maquina_disponivel_stream_tambem_mostra_o_nome_pedido(reserva):
    reserva["machine"] = POD
    sse = (
        b'data: {"id":"x","model":"pro-base","choices":[{"delta":{"content":"ol"}}]}\n\n'
        b'data: {"id":"x","model":"pro-base","choices":[{"delta":{"content":"\xc3\xa1"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    reserva["pod_resposta"] = lambda r: httpx.Response(
        200, content=sse, headers={"content-type": "text/event-stream"}
    )
    r = _chat(reserva, model=TEXT_SLUG, stream=True)
    assert b'"model":"pro-base"' not in r.content
    assert r.content.count(('"model":"' + TEXT_SLUG + '"').encode()) == 2


def test_maquina_lotada_desvia(reserva, monkeypatch):
    reserva["machine"] = POD

    def lotada(flight_key, *a, **k):
        main.release_flight(flight_key)
        raise main.HTTPException(status_code=429, detail="lotada")

    monkeypatch.setattr(main, "check_concurrency", lotada)
    r = _chat(reserva, model="pro-base")
    assert r.status_code == 200
    assert reserva["sent"][0][1]["model"] == QWEN and reserva["pod"] == []


def test_conversa_maior_que_a_maquina_continua_no_openrouter(reserva, monkeypatch):
    reserva["machine"] = POD

    async def estourou(*a, **k):
        raise main.ContextWindowExceeded("não cabe")

    monkeypatch.setattr(main, "validate_body", estourou)
    r = _chat(reserva, model=TEXT_SLUG)
    assert r.status_code == 200
    assert reserva["sent"][0][1]["model"] == TEXT_SLUG and reserva["pod"] == []
    # o corpo desviado é o ORIGINAL do cliente, não o reescrito para a máquina
    assert reserva["sent"][0][1]["messages"][-1] == {"role": "user", "content": "oi"}


def test_maquinas_desligadas_pro_base_vai_pro_reserva(reserva):
    reserva["settings"]["machines_enabled"] = False
    r = _chat(reserva, model="pro-base")
    assert r.status_code == 200
    assert reserva["sent"][0][1]["model"] == QWEN
    assert reserva["resolve_calls"] == []


def test_sem_reserva_e_sem_maquina_segue_o_503_da_maquina(reserva):
    reserva["fallback"] = None
    r = _chat(reserva, model="pro-base")
    assert r.status_code == 503 and reserva["sent"] == []


def test_messages_sem_maquina_desvia_e_com_maquina_nao(reserva):
    corpo = {"model": TEXT_SLUG, "max_tokens": 100,
             "messages": [{"role": "user", "content": "oi"}]}
    r = reserva["client"].post("/v1/messages", json=corpo, headers={"x-api-key": "sk-cliente"})
    assert r.status_code == 200
    assert reserva["sent"][0][0].url.path == "/api/v1/messages"
    assert reserva["sent"][0][1]["model"] == TEXT_SLUG


def test_messages_contexto_maior_que_a_maquina_desvia(reserva, monkeypatch):
    reserva["machine"] = POD

    async def estourou(*a, **k):
        raise main.ContextWindowExceeded("não cabe")

    monkeypatch.setattr(main, "validate_body", estourou)
    r = reserva["client"].post(
        "/v1/messages",
        json={"model": "pro-base", "max_tokens": 100, "messages": [{"role": "user", "content": "oi"}]},
        headers={"x-api-key": "sk-cliente"},
    )
    assert r.status_code == 200
    assert reserva["sent"][0][1]["model"] == QWEN and reserva["pod"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_nome_pedido_sobrevive_ao_filtro_de_think_do_pro(reserva, stream):
    reserva["machine"] = POD
    reserva["plan"] = "Pro"  # REASONING_LEAK_PLANS: resposta passa pelo filtro de <think>
    if stream:
        sse = (
            b'data: {"id":"x","model":"pro-base","choices":[{"index":0,"delta":{"content":"<think>hmm</think>"}}]}\n\n'
            b'data: {"id":"x","model":"pro-base","choices":[{"index":0,"delta":{"content":"ol\xc3\xa1"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        reserva["pod_resposta"] = lambda r: httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"})
    else:
        reserva["pod_resposta"] = lambda r: httpx.Response(200, json={
            "id": "x", "model": "pro-base",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "<think>hmm</think>olá"},
                         "finish_reason": "stop"}],
        })
    r = _chat(reserva, model=TEXT_SLUG, stream=stream)
    assert r.status_code == 200
    assert b"pro-base" not in r.content
    assert TEXT_SLUG.encode() in r.content
    if not stream:
        assert r.json()["choices"][0]["message"]["content"] == "olá"


def test_models_sem_maquina_lista_o_openrouter(reserva):
    r = reserva["client"].get("/v1/models", headers={"Authorization": "Bearer sk-cliente"})
    assert r.status_code == 200
    assert QWEN in [m["id"] for m in r.json()["data"]]


@pytest.mark.parametrize("rota_", ["chat", "messages"])
def test_maquina_inalcancavel_desvia(reserva, rota_):
    reserva["machine"] = POD

    def cai(request):
        raise httpx.ConnectError("pod fora do ar")

    reserva["pod_resposta"] = cai
    if rota_ == "chat":
        r = _chat(reserva, model="pro-base")
    else:
        r = reserva["client"].post(
            "/v1/messages",
            json={"model": "pro-base", "max_tokens": 100, "messages": [{"role": "user", "content": "oi"}]},
            headers={"x-api-key": "sk-cliente"},
        )
    assert r.status_code == 200
    assert reserva["sent"][0][1]["model"] == QWEN


@pytest.mark.parametrize("rota_", ["chat", "messages"])
def test_maquina_503_antes_do_primeiro_byte_desvia(reserva, rota_):
    reserva["machine"] = POD
    reserva["pod_resposta"] = lambda r: httpx.Response(503, json={"detail": "vLLM reiniciando"})
    if rota_ == "chat":
        r = _chat(reserva, model="pro-base", stream=True)
    else:
        r = reserva["client"].post(
            "/v1/messages",
            json={"model": "pro-base", "max_tokens": 100, "stream": True,
                  "messages": [{"role": "user", "content": "oi"}]},
            headers={"x-api-key": "sk-cliente"},
        )
    assert r.status_code == 200
    assert reserva["sent"][0][1]["model"] == QWEN


def test_maquina_503_sem_reserva_repassa_o_erro(reserva):
    reserva["machine"] = POD
    reserva["fallback"] = None
    reserva["pod_resposta"] = lambda r: httpx.Response(503, json={"detail": "vLLM reiniciando"})
    r = _chat(reserva, model="pro-base")
    assert r.status_code == 503 and reserva["sent"] == []


# ---------------------------------------------------------------------------
# acesso a modelo e teto de contexto por plano (01/10, migration 0074)
# ---------------------------------------------------------------------------

QWEN9 = "qwen/qwen3.5-9b"
GLM = "z-ai/glm-5.3-flash"
TODOS = ["Go", "Pro", "Max", "Enterprise"]
GO_POD = {
    "id": "m-go", "public_url": "https://pod-go.test", "served_model_name": "go-base",
    "model_name": "Qwen/Qwen3.5-9B", "max_model_len": 65536, "max_concurrent_seqs": 16,
    "admin_secret": "segredo-do-pod",
}


@pytest.fixture
def planos(reserva):
    reserva["catalog"].update({QWEN9: "text", GLM: "text"})
    reserva["plans"] = {
        QWEN9: TODOS, GLM: TODOS, TEXT_SLUG: TODOS, IMAGE_SLUG: TODOS,
        QWEN: ["Pro", "Max", "Enterprise"],
    }
    reserva["fallback_plans"] = {QWEN9: ["Go"], QWEN: ["Pro", "Max", "Enterprise"]}
    reserva["plan"] = "Go"
    return reserva


def _texto_de_tokens(n: int) -> str:
    # estimate_prompt_tokens: ~4 caracteres por token
    return "abcd" * n


def _pod_que_conta(tokens: int):
    """Pod que responde o /admin/tokenize com `tokens` e o chat com 200."""
    def resposta(request):
        if request.url.path == "/admin/tokenize":
            return httpx.Response(200, json={"count": tokens})
        return httpx.Response(200, json={
            "id": "x", "model": "go-base",
            # Go/Pro passam pelo filtro de <think> (REASONING_LEAK_PLANS)
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": "<think>hmm</think>olá"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens, "completion_tokens": 1},
        })
    return resposta


def _chamadas_de_chat(estado):
    return [b for b in estado["pod"] if "messages" in b]


@pytest.mark.parametrize("repasse", [True, False])
def test_go_pedindo_modelo_do_pro_e_403_sem_ligar_maquina(planos, repasse):
    planos["settings"]["openrouter_enabled"] = repasse
    r = _chat(planos, model=QWEN)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "plano Go" in detail and QWEN9 in detail and GLM in detail
    assert planos["resolve_calls"] == [] and planos["sent"] == []


def test_go_pedindo_modelo_do_pro_no_messages_e_403(planos):
    r = planos["client"].post(
        "/v1/messages",
        json={"model": QWEN, "max_tokens": 100, "messages": [{"role": "user", "content": "oi"}]},
        headers={"x-api-key": "sk-cliente"},
    )
    assert r.status_code == 403 and planos["sent"] == []


@pytest.mark.parametrize("plano", ["Go", "Pro", "Max"])
def test_kimi_k3_bloqueado_para_todos_mesmo_com_maquina(planos, plano):
    # sem plano nenhum: nem a máquina (que responderia qualquer nome) atende
    planos["catalog"]["moonshotai/kimi-k3"] = "text"
    planos["plans"]["moonshotai/kimi-k3"] = []
    planos["entry"] = _entry(plan=plano)
    planos["plan"] = plano
    planos["machine"] = GO_POD
    r = _chat(planos, model="moonshotai/kimi-k3")
    assert r.status_code == 403
    assert r.json()["detail"] == "o modelo 'moonshotai/kimi-k3' ainda não está disponível"
    assert planos["resolve_calls"] == [] and planos["sent"] == [] and planos["pod"] == []


def test_pro_alcanca_o_27b(planos):
    planos["entry"] = _entry(plan="Pro")
    r = _chat(planos, model=QWEN)
    assert r.status_code == 200 and planos["sent"][0][1]["model"] == QWEN


def test_go_sem_maquina_cai_no_reserva_do_go(planos):
    _chat(planos, model="go-base")
    assert planos["sent"][0][1]["model"] == QWEN9


def test_pro_sem_maquina_cai_no_reserva_do_pro(planos):
    planos["entry"] = _entry(plan="Pro")
    _chat(planos, model="pro-base")
    assert planos["sent"][0][1]["model"] == QWEN


def test_models_do_go_nao_lista_o_27b(planos):
    r = planos["client"].get("/v1/models", headers={"Authorization": "Bearer sk-cliente"})
    ids = [m["id"] for m in r.json()["data"]]
    assert QWEN9 in ids and GLM in ids and QWEN not in ids


def test_go_acima_de_32k_na_maquina_e_400_sem_desvio(planos):
    # máquina do Go tem 64K, mas o plano é 32K: o estouro é do PLANO e não vai
    # para o OpenRouter (lá também não caberia)
    planos["machine"] = GO_POD
    planos["pod_resposta"] = _pod_que_conta(40000)
    r = _chat(planos, model="go-base", messages=[
        {"role": "user", "content": _texto_de_tokens(40000)},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "context_length_exceeded"
    assert "32768" in r.json()["error"]["message"]
    assert planos["sent"] == [] and _chamadas_de_chat(planos) == []


def test_go_abaixo_de_32k_na_maquina_passa(planos):
    planos["machine"] = GO_POD
    planos["pod_resposta"] = _pod_que_conta(20000)
    r = _chat(planos, model="go-base", max_tokens=30000, messages=[
        {"role": "user", "content": _texto_de_tokens(20000)},
    ])
    assert r.status_code == 200
    enviado = _chamadas_de_chat(planos)[0]
    # saída clampada ao que sobra dos 32K, não dos 64K da máquina
    assert enviado["max_tokens"] <= 32768 - 20000


def test_pro_acima_da_maquina_desvia_e_cabe_nos_256k(planos):
    planos["entry"] = _entry(plan="Pro")
    planos["plan"] = "Pro"
    planos["machine"] = {**POD, "admin_secret": "segredo-do-pod"}  # 128K
    planos["pod_resposta"] = _pod_que_conta(140000)
    r = _chat(planos, model="pro-base", messages=[
        {"role": "user", "content": _texto_de_tokens(140000)},
    ])
    assert r.status_code == 200
    assert planos["sent"][0][1]["model"] == QWEN and _chamadas_de_chat(planos) == []


def test_go_acima_de_32k_no_openrouter_e_400(planos):
    planos["settings"]["machines_enabled"] = False
    r = _chat(planos, model=QWEN9, messages=[
        {"role": "user", "content": _texto_de_tokens(40000)},
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "context_length_exceeded"
    assert planos["sent"] == []


def test_go_no_openrouter_clampa_a_saida_ao_plano(planos):
    planos["settings"]["machines_enabled"] = False
    r = _chat(planos, model=QWEN9, max_tokens=30000, messages=[
        {"role": "user", "content": _texto_de_tokens(20000)},
    ])
    assert r.status_code == 200
    # abaixo do teto do repasse (OPENROUTER_MAX_TOKENS): quem cortou foi o plano
    assert planos["sent"][0][1]["max_tokens"] < 32768 - 20000


def test_pro_acima_de_256k_no_openrouter_e_400_no_formato_anthropic(planos):
    planos["entry"] = _entry(plan="Pro")
    planos["settings"]["machines_enabled"] = False
    r = planos["client"].post(
        "/v1/messages",
        json={"model": "pro-base", "max_tokens": 100,
              "messages": [{"role": "user", "content": _texto_de_tokens(270000)}]},
        headers={"x-api-key": "sk-cliente"},
    )
    assert r.status_code == 400
    assert r.json()["type"] == "error" and "262144" in r.json()["error"]["message"]
    assert planos["sent"] == []


# ---------------------------------------------------------------------------
# /v1/decisions (Jev, da TypeSafe): só OpenRouter, no /api/alpha/decisions
# ---------------------------------------------------------------------------

JEV = "typesafe/jev-1.13"
PERGUNTAS = {
    "time": {
        "type": "choice",
        "instructions": "Qual time deve cuidar do ticket?",
        "criteria": {"pagamentos": "checkout e cobrança", "frontend": "renderização"},
    },
}


@pytest.fixture
def decisao(rota):
    rota["catalog"][JEV] = "decisions"
    rota["resposta"] = lambda r: httpx.Response(200, json={
        "id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
        "answers": {"time": {"type": "choice", "choice": "pagamentos", "confidence": 0.67,
                             "probabilities": {"pagamentos": 0.78, "frontend": 0.22}}},
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    })
    return rota


def _decidir(estado, **body):
    return estado["client"].post(
        "/v1/decisions",
        json={"state": {"ticket": "a tela fica branca no Pagar"}, "questions": PERGUNTAS, **body},
        headers={"Authorization": "Bearer sk-cliente"},
    )


def test_decisions_vai_para_o_alpha_do_openrouter_sem_custo(decisao):
    r = _decidir(decisao, model=JEV, stream=True)
    assert r.status_code == 200
    assert r.json()["answers"]["time"]["choice"] == "pagamentos"
    assert "cost" not in r.json()["usage"]

    request, sent = decisao["sent"][0]
    # fora do /api/v1: a API de Decisions é alpha e mora em /api/alpha
    assert str(request.url) == "https://openrouter.test/api/alpha/decisions"
    assert request.headers["authorization"] == "Bearer sk-or-da-stac"
    assert sent["model"] == JEV and "stream" not in sent
    assert sent["questions"] == PERGUNTAS

    log = decisao["logged"][0]
    assert log["path"] == "decisions" and log["model"] == JEV
    assert log["upstream"] == "openrouter" and log["machine_id"] is None
    assert log["stream"] is False and log["status_code"] == 200
    assert log["usage"]["prompt_tokens"] == 476 and log["cost_usd"] == 0.000019992
    # nenhuma máquina serve decisões: nem é consultada
    assert decisao["resolve_calls"] == []


def test_decisions_url_configuravel(decisao, monkeypatch):
    monkeypatch.setattr(main, "OPENROUTER_DECISIONS_URL", "https://openrouter.test/api/beta/decisions")
    assert _decidir(decisao, model=JEV).status_code == 200
    assert decisao["sent"][0][0].url.path == "/api/beta/decisions"


def test_decisions_com_modelo_de_texto_e_404_com_os_aceitos(decisao):
    r = _decidir(decisao, model=TEXT_SLUG)
    assert r.status_code == 404
    assert JEV in r.json()["detail"] and TEXT_SLUG not in r.json()["detail"].split("aceitos:")[1]
    assert decisao["sent"] == [] and decisao["resolve_calls"] == []


def test_decisions_sem_modelo_e_404(decisao):
    r = _decidir(decisao)
    assert r.status_code == 404 and JEV in r.json()["detail"]
    assert decisao["sent"] == []


def test_decisions_com_repasse_desligado_e_404(decisao):
    decisao["settings"]["openrouter_enabled"] = False
    r = _decidir(decisao, model=JEV)
    assert r.status_code == 404 and "não está disponível no momento" in r.json()["detail"]
    assert decisao["sent"] == []


def test_decisions_fora_do_plano_e_403(decisao):
    decisao["plans"] = {TEXT_SLUG: ["Go"], IMAGE_SLUG: ["Go"], JEV: ["Pro"]}
    decisao["fallback_plans"] = {}
    r = _decidir(decisao, model=JEV)
    assert r.status_code == 403 and "plano Go" in r.json()["detail"]
    assert decisao["sent"] == []


def test_decisions_erro_do_cliente_passa_com_o_status(decisao):
    decisao["resposta"] = lambda r: httpx.Response(400, json={
        "error": {"code": 400, "message": "questions is required"},
    })
    r = _decidir(decisao, model=JEV)
    assert r.status_code == 400 and r.json()["error"]["message"] == "questions is required"
    assert decisao["logged"][0]["status_code"] == 400


def test_jev_no_chat_nao_e_repassado_como_texto(decisao):
    decisao["settings"]["machines_enabled"] = False
    r = _chat(decisao, model=JEV)
    assert r.status_code == 404
    assert TEXT_SLUG in r.json()["detail"] and JEV not in r.json()["detail"].split("aceitos:")[1]
    assert decisao["sent"] == []
