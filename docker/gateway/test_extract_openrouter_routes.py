"""Wiring HTTP de /v1/documents/extract e /v1/images/extract com as MÁQUINAS
DESLIGADAS — a inferência vai ao OpenRouter.

    python3 -m pytest test_extract_openrouter_routes.py

Desde o desligamento das máquinas (system_settings.machines_enabled = false,
28/09/2026), as duas rotas devolviam 503 "inferência nas máquinas da Stac
temporariamente desligada" antes mesmo do OCR, porque chamavam resolve_route
direto — e um cliente real recebeu esse erro em produção. Aqui testa-se a
LIGAÇÃO que corrige isso: com máquinas off e repasse on, o OCR continua no
gateway e o chat/completions final vai ao reserva de texto do plano no
OpenRouter, sem os campos que só o vLLM entende; com máquinas on, o caminho
antigo de pod segue intacto; com os dois off, o 503 de sempre.

authenticate/resolve_route reais ficam de fora (dependem de Supabase e de
estado de máquina). Com as máquinas desligadas, resolve_route é um duplo que
FALHA o teste se for chamado: nenhuma extração pode tentar ligar máquina.
"""

import json
import os

import pytest

pytest.importorskip("fastapi", reason="wiring HTTP exige fastapi instalado")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import document_extract  # noqa: E402
import main  # noqa: E402

QWEN_GO = "qwen/qwen3.5-9b"
QWEN_PRO = "qwen/qwen3.8-27b"
MACHINE = {
    "id": "mach-1",
    "public_url": "https://pod.example",
    "served_model_name": "pro-base",
    "model_name": "Qwen/Qwen3.8-27B",
    "max_model_len": 32768,
}
STACK = {"id": "stack-1", "plan": "Pro", "category": "llm", "system_prompt": None}
ENTRY = {
    "api_key_id": "key-1", "account_id": "acc-1", "purpose": "customer",
    "stack_id": "stack-1", "stacks": [STACK],
}
SCHEMA = {
    "type": "object",
    "properties": {"total": {"type": "number"}},
    "required": ["total"],
}
HEADERS = {"Authorization": "Bearer sk-teste"}
PDF_PATH = "/v1/documents/extract"
IMG_PATH = "/v1/images/extract"
PNG = b"\x89PNG\r\n\x1a\nconteudo"


class FakeSupa:
    def __init__(self, estado):
        self.estado = estado

    async def get_setting(self, key, default):
        return self.estado["settings"].get(key, default)

    async def list_enabled_openrouter_models(self):
        # reserva de texto por plano (0074): Go cai no 9B, Pro/Max no 27B
        return [
            {"slug": QWEN_GO, "kind": "text", "fallback": True,
             "plans": ["Go", "Pro", "Max"], "fallback_plans": ["Go"]},
            {"slug": QWEN_PRO, "kind": "text", "fallback": True,
             "plans": ["Pro", "Max"], "fallback_plans": ["Pro", "Max"]},
        ]


def _ok_choice(content='{"total": 10}', finish_reason="stop"):
    return httpx.Response(200, json={
        "id": "gen-1",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "cost": 0.00012, "is_byok": False},
    })


@pytest.fixture
def ctx(monkeypatch):
    estado = {
        "settings": {"machines_enabled": False, "openrouter_enabled": True},
        "plan": "Pro",
        "openrouter": [],  # (request, body) do que chegou ao OpenRouter
        "pod": [],  # idem, ao pod
        "resposta": lambda request: _ok_choice(),  # callable(request) -> Response do OpenRouter
        "logged": [],
    }
    supa = FakeSupa(estado)
    monkeypatch.setattr(main, "supa", supa, raising=False)

    async def fake_authenticate(authorization, headers, path=None):
        return ENTRY, "hash-1"

    async def fake_quota(*a, **k):
        return None

    async def resolve_route_proibido(account_id, entry):
        raise AssertionError("resolve_route não pode ser chamado com as máquinas desligadas")

    async def fake_touch(stack_id, machine_id=None):
        return None

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "check_rate_limit", lambda key, plan: None)
    monkeypatch.setattr(main, "check_token_quota", fake_quota)
    monkeypatch.setattr(main, "check_request_quota", fake_quota)
    monkeypatch.setattr(
        main, "resolve_key_stack", lambda entry: (dict(STACK, plan=estado["plan"]), estado["plan"])
    )
    monkeypatch.setattr(main, "resolve_route", resolve_route_proibido)
    monkeypatch.setattr(main, "maybe_touch", fake_touch)
    monkeypatch.setattr(main, "resolve_system_prompt", lambda entry, stack: None)
    monkeypatch.setattr(main, "log_gateway_request", lambda **kw: estado["logged"].append(kw))

    # OCR falso: sem tesseract, só o texto que o teste espera ver no prompt
    monkeypatch.setattr(
        document_extract, "extract_text_many",
        lambda documents, plan: ([(name, f"TEXTO DE {name}") for name, _ in documents], 1, False),
    )
    monkeypatch.setattr(
        document_extract, "extract_text_from_images",
        lambda images, plan: ([(name, f"TEXTO DE {name}") for name, _ in images], True),
    )

    async def openrouter_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        estado["openrouter"].append((request, body))
        return estado["resposta"](request)

    async def pod_handler(request: httpx.Request) -> httpx.Response:
        estado["pod"].append((request, json.loads(await request.aread())))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"total": 7}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    monkeypatch.setattr(
        main, "openrouter_client",
        httpx.AsyncClient(
            base_url="https://openrouter.test/api/v1",
            headers={"Authorization": "Bearer sk-or-da-stac"},
            transport=httpx.MockTransport(openrouter_handler),
        ),
    )
    monkeypatch.setattr(
        main, "document_client",
        httpx.AsyncClient(transport=httpx.MockTransport(pod_handler)),
        raising=False,
    )
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "sk-or-da-stac")
    # chaves espelho desligadas: o repasse usa a compartilhada
    monkeypatch.setattr(main, "openrouter_mgmt_client", None)
    monkeypatch.setattr(main, "openrouter_secret_box", None)
    main.settings_cache.clear()
    monkeypatch.setattr(main, "openrouter_catalog_cache", None)
    main.in_flight.clear()
    estado["client"] = TestClient(main.app)
    yield estado
    assert sum(main.in_flight.values()) == 0  # nenhum caminho vaza vaga
    main.in_flight.clear()
    main.settings_cache.clear()


def _post(ctx, path, files, **data):
    return ctx["client"].post(
        path, files=files, data={"schema": json.dumps(SCHEMA), **data}, headers=HEADERS
    )


def _pdf_files(name="nota.pdf"):
    return [("file", (name, b"%PDF-1.4 falso", "application/pdf"))]


def _img_files(name="foto.png"):
    return [("file", (name, PNG, "image/png"))]


# ---------------------------------------------------------------------------
# máquinas desligadas + repasse ligado: a inferência vai ao OpenRouter
# ---------------------------------------------------------------------------


def test_pdf_sem_maquinas_vai_ao_reserva_do_plano_sem_campos_do_vllm(ctx):
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 200, r.text
    assert r.json() == {
        "data": {"total": 10}, "pages": 1, "files": 1, "ocr_used": False,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "cost": 0.00012, "is_byok": False},
    }
    assert ctx["pod"] == []
    (request, body), = ctx["openrouter"]
    assert request.url.path == "/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-or-da-stac"
    assert body["model"] == QWEN_PRO  # reserva de texto do Pro, não do Go
    assert "chat_template_kwargs" not in body  # só o vLLM entende
    assert body["stream"] is False
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == main.DOCUMENT_MAX_TOKENS
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "TEXTO DE nota.pdf" in body["messages"][-1]["content"]


def test_go_cai_no_reserva_do_go(ctx):
    ctx["plan"] = "Go"
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 200, r.text
    assert ctx["openrouter"][0][1]["model"] == QWEN_GO


def test_imagem_sem_maquinas_tambem_vai_ao_openrouter(ctx):
    r = _post(ctx, IMG_PATH, _img_files())
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"total": 10}
    assert r.json()["ocr_used"] is True
    assert ctx["pod"] == []
    body = ctx["openrouter"][0][1]
    assert body["model"] == QWEN_PRO
    assert "chat_template_kwargs" not in body
    assert "TEXTO DE foto.png" in body["messages"][-1]["content"]


def test_log_registra_upstream_openrouter_sem_maquina_e_com_custo(ctx):
    _post(ctx, PDF_PATH, _pdf_files())
    (log,) = ctx["logged"]
    assert log["status_code"] == 200
    assert log["upstream"] == "openrouter"
    assert log["machine_id"] is None
    assert log["model"] == QWEN_PRO
    assert log["path"] == "documents/extract"
    assert log["usage"]["completion_tokens"] == 5
    assert log["cost_usd"] == pytest.approx(0.00012)


def test_provedor_recusa_response_format_e_a_extracao_repete_sem_ele(ctx):
    def resposta(request):
        if len(ctx["openrouter"]) == 1:
            return httpx.Response(400, json={"error": {
                "message": "response_format is not supported by this provider"}})
        return _ok_choice('```json\n{"total": 42}\n```')

    ctx["resposta"] = resposta
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"total": 42}  # cerca de código tolerada
    primeira, segunda = (b for _, b in ctx["openrouter"])
    assert "response_format" in primeira
    assert "response_format" not in segunda
    # sem gramática, o schema e a exigência de só-JSON vão no prompt
    assert "Responda SOMENTE com um objeto JSON" in segunda["messages"][-1]["content"]
    assert json.dumps(SCHEMA, ensure_ascii=False) in segunda["messages"][-1]["content"]
    assert segunda["messages"][-1]["content"].index("TEXTO DE nota.pdf") < \
        segunda["messages"][-1]["content"].index("Responda SOMENTE")


def test_saida_nao_aderente_continua_502_com_o_texto_cru(ctx):
    ctx["resposta"] = lambda r: _ok_choice('{"total": "dez"}')
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 502
    assert r.json()["detail"]["message"] == "o modelo não devolveu JSON aderente ao schema"
    assert r.json()["detail"]["raw_output"] == '{"total": "dez"}'
    assert ctx["logged"][-1]["status_code"] == 502
    assert ctx["logged"][-1]["cost_usd"] == pytest.approx(0.00012)


def test_truncada_por_tokens_e_400(ctx):
    ctx["resposta"] = lambda r: _ok_choice('{"total": 1', finish_reason="length")
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 400
    assert "max_tokens_disponivel" in r.json()["detail"]


@pytest.mark.parametrize("status", [401, 402])
def test_problema_da_conta_da_stac_vira_503_generico(ctx, status):
    ctx["resposta"] = lambda r: httpx.Response(status, json={"error": {"message": "sem crédito"}})
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 503
    assert "sem crédito" not in r.text
    assert ctx["logged"][-1]["status_code"] == status


def test_erro_do_provedor_vira_502_generico(ctx):
    ctx["resposta"] = lambda r: httpx.Response(500, json={"error": {"message": "provider down"}})
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 502
    assert r.json()["detail"] == "falha ao processar o documento no modelo"
    assert len(ctx["openrouter"]) == 1  # 500 não é "recusou response_format": não repete


def test_200_so_com_error_vira_502(ctx):
    ctx["resposta"] = lambda r: httpx.Response(200, json={"error": {"message": "falhou depois"}})
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 502


def test_openrouter_inalcancavel_e_503(ctx):
    def cai(request):
        raise httpx.ConnectError("sem rede")

    ctx["resposta"] = cai
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 503
    assert "tente novamente" in r.json()["detail"]


def test_teto_de_contexto_do_repasse_e_configuravel(ctx, monkeypatch):
    # janela minúscula: o conteúdo não deixa espaço pra resposta → 400 antes
    # de gastar uma chamada ao OpenRouter
    monkeypatch.setattr(main, "OPENROUTER_EXTRACT_CONTEXT_TOKENS", 64)
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 400
    assert "64 tokens" in r.text
    assert ctx["openrouter"] == []


def test_sem_chave_nenhuma_e_503(ctx, monkeypatch):
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 503
    assert ctx["openrouter"] == []


# ---------------------------------------------------------------------------
# máquinas ligadas: caminho antigo intacto
# ---------------------------------------------------------------------------


def test_maquinas_ligadas_segue_para_o_pod(ctx, monkeypatch):
    ctx["settings"]["machines_enabled"] = True

    async def fake_resolve_route(account_id, entry):
        return MACHINE, False, ctx["plan"], STACK["id"]

    monkeypatch.setattr(main, "resolve_route", fake_resolve_route)
    monkeypatch.setattr(main, "check_concurrency", lambda *a, **k: None)
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"total": 7}
    assert ctx["openrouter"] == []
    (request, body), = ctx["pod"]
    assert request.url == "https://pod.example/v1/chat/completions"
    assert body["model"] == "pro-base"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}  # o vLLM ainda recebe
    assert ctx["logged"][-1]["machine_id"] == "mach-1"
    assert ctx["logged"][-1].get("upstream") is None


# ---------------------------------------------------------------------------
# tudo desligado: o 503 de sempre, sem tocar em nada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,files", [(PDF_PATH, _pdf_files()), (IMG_PATH, _img_files())])
def test_tudo_desligado_e_503(ctx, path, files):
    ctx["settings"]["openrouter_enabled"] = False
    r = _post(ctx, path, files)
    assert r.status_code == 503
    assert r.json()["detail"] == "inferência nas máquinas da Stac temporariamente desligada"
    assert ctx["openrouter"] == [] and ctx["pod"] == []


def test_plano_sem_reserva_de_texto_e_503(ctx):
    ctx["plan"] = "Enterprise"  # fora de todo fallback_plans do catálogo
    r = _post(ctx, PDF_PATH, _pdf_files())
    assert r.status_code == 503
    assert ctx["openrouter"] == []
