"""Wiring HTTP de /v1/documents/extract e /v1/images/extract com VÁRIOS
arquivos — o que os módulos puros não cobrem.

    python3 -m pytest test_extract_routes.py

document_extract testa DECISÕES (tetos, ordem, prompt). Aqui testa-se a
LIGAÇÃO: como os uploads são coletados do multipart, em que ordem os tetos do
plano são aplicados e o que chega ao pod e volta ao cliente.

O bug que este arquivo existe para impedir: antes do multi-arquivo, dois
`-F file=@...` faziam o FastAPI ficar com o ÚLTIMO e descartar o primeiro em
silêncio — o cliente recebia um JSON válido extraído de metade do que mandou,
sem erro nenhum. Agora os dois aparecem, e é o plano que decide.

authenticate/resolve_route reais ficam de fora (dependem de Supabase e de
estado de máquina): são substituídos por duplos que devolvem o plano escolhido
pelo teste. O OCR também: extract_text_many/extract_text_from_images são
espionados para o teste ver COM QUAIS arquivos foram chamados e em que ordem.
"""

import json
import os

import pytest

pytest.importorskip("fastapi", reason="wiring HTTP exige fastapi instalado")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")
fitz = pytest.importorskip("fitz", reason="PDF de teste exige pymupdf")

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import document_extract  # noqa: E402
import main  # noqa: E402

MACHINE = {
    "id": "mach-1",
    "public_url": "https://pod.example",
    "served_model_name": "pro-base",
    "model_name": "Qwen/Qwen3.8-27B",
    "max_model_len": 32768,
}
ENTRY = {"api_key_id": "key-1", "account_id": "acc-1", "purpose": "customer"}
STACK_ID = "stack-1"
SCHEMA = json.dumps({
    "type": "object",
    "properties": {"total": {"type": "number"}},
    "required": ["total"],
})
HEADERS = {"Authorization": "Bearer sk-teste"}


def _pdf(*pages_text: str) -> bytes:
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


@pytest.fixture
def ctx(monkeypatch):
    """Rotas de extração prontas para chamar, com plano configurável e pod
    falso que devolve um JSON aderente ao schema."""
    state = {"plan": "Pro", "prompts": [], "extracted": []}

    async def fake_authenticate(authorization, headers, path=None):
        return ENTRY, "hash-1"

    async def fake_quota(account_id, plan, purpose="customer"):
        return None

    async def fake_resolve_route(account_id, entry):
        return MACHINE, False, state["plan"], STACK_ID

    async def fake_touch(stack_id, machine_id=None):
        return None

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "check_rate_limit", lambda key, plan: None)
    monkeypatch.setattr(main, "check_token_quota", fake_quota)
    monkeypatch.setattr(main, "resolve_key_stack", lambda entry: (None, state["plan"]))
    monkeypatch.setattr(main, "resolve_route", fake_resolve_route)
    monkeypatch.setattr(main, "maybe_touch", fake_touch)
    monkeypatch.setattr(main, "check_concurrency", lambda *a, **k: None)
    monkeypatch.setattr(main, "resolve_system_prompt", lambda entry, stack: None)
    monkeypatch.setattr(main, "log_gateway_request", lambda **kw: None)

    # espiões do OCR: registram os arquivos recebidos e devolvem o texto sem
    # tesseract (imagem) ou via extração real (PDF digital, sem OCR)
    real_many = document_extract.extract_text_many

    def spy_many(documents, plan):
        state["extracted"].append([name for name, _ in documents])
        return real_many(documents, plan)

    def spy_images(images, plan):
        state["extracted"].append([name for name, _ in images])
        return [(name, f"TEXTO DE {name}") for name, _ in images], True

    monkeypatch.setattr(document_extract, "extract_text_many", spy_many)
    monkeypatch.setattr(document_extract, "extract_text_from_images", spy_images)

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        state["prompts"].append(body["messages"][-1]["content"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"total": 10}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    monkeypatch.setattr(
        main, "document_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        raising=False,
    )
    main.in_flight.clear()
    yield type("Ctx", (), {"client": TestClient(main.app), "state": state})
    main.in_flight.clear()


def _post(ctx, path, files, **data):
    return ctx.client.post(
        path, files=files, data={"schema": SCHEMA, **data}, headers=HEADERS
    )


PDF_PATH = "/v1/documents/extract"
IMG_PATH = "/v1/images/extract"
PNG = b"\x89PNG\r\n\x1a\nconteudo"


# ---------------------------------------------------------------------------
# contrato original intacto
# ---------------------------------------------------------------------------


def test_um_arquivo_continua_funcionando_em_todo_plano(ctx):
    ctx.state["plan"] = "Go"
    r = _post(ctx, PDF_PATH, [("file", ("nota.pdf", _pdf("TOTAL 10"), "application/pdf"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == {"total": 10}
    assert body["pages"] == 1
    assert body["files"] == 1
    assert body["ocr_used"] is False
    # um arquivo só: bloco único de sempre, sem numeração
    assert "DOCUMENTO 1 DE" not in ctx.state["prompts"][0]


def test_sem_arquivo_e_422(ctx):
    r = ctx.client.post(PDF_PATH, data={"schema": SCHEMA}, headers=HEADERS)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Go: um arquivo por requisição
# ---------------------------------------------------------------------------


def test_go_com_dois_arquivos_e_413_sem_extrair_nada(ctx):
    """A recusa tem que vir ANTES da extração: nada do Go deve pagar OCR por
    uma requisição que o plano não aceita. E a mensagem diz o caminho (um por
    requisição, ou plano a partir do Pro) — não "arquivo grande demais"."""
    ctx.state["plan"] = "Go"
    r = _post(ctx, PDF_PATH, [
        ("files", ("a.pdf", _pdf("A"), "application/pdf")),
        ("files", ("b.pdf", _pdf("B"), "application/pdf")),
    ])
    assert r.status_code == 413, r.text
    assert "1 arquivo por requisição" in r.json()["detail"]
    assert "Pro" in r.json()["detail"]
    assert ctx.state["extracted"] == []
    assert sum(main.in_flight.values()) == 0


def test_dois_campos_file_nao_sao_mais_descartados_em_silencio(ctx):
    """O bug original: `-F file=@a -F file=@b` no Go devolvia 200 com só o
    segundo. Agora os dois são vistos e o plano recusa explicitamente."""
    ctx.state["plan"] = "Go"
    r = _post(ctx, PDF_PATH, [
        ("file", ("a.pdf", _pdf("A"), "application/pdf")),
        ("file", ("b.pdf", _pdf("B"), "application/pdf")),
    ])
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Pro: vários arquivos numa requisição
# ---------------------------------------------------------------------------


def test_pro_com_dois_pdfs_devolve_um_json_com_os_dois_no_prompt(ctx):
    r = _post(ctx, PDF_PATH, [
        ("files", ("fatura.pdf", _pdf("FATURA 1", "pagina 2"), "application/pdf")),
        ("files", ("recibo.pdf", _pdf("RECIBO 2"), "application/pdf")),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files"] == 2
    assert body["pages"] == 3, "páginas são a SOMA dos arquivos"
    prompt = ctx.state["prompts"][0]
    assert "DOCUMENTO 1 DE 2 (fatura.pdf)" in prompt
    assert "DOCUMENTO 2 DE 2 (recibo.pdf)" in prompt
    assert prompt.index("FATURA 1") < prompt.index("RECIBO 2"), "ordem de envio preservada"


def test_file_e_files_se_combinam_na_ordem(ctx):
    """`file` (contrato antigo) e `files` na mesma requisição: tudo entra,
    `file` primeiro — quem migra um cliente aos poucos não perde nada."""
    r = _post(ctx, PDF_PATH, [
        ("file", ("um.pdf", _pdf("UM"), "application/pdf")),
        ("files", ("dois.pdf", _pdf("DOIS"), "application/pdf")),
        ("files", ("tres.pdf", _pdf("TRES"), "application/pdf")),
    ])
    assert r.status_code == 200, r.text
    assert r.json()["files"] == 3
    assert ctx.state["extracted"] == [["um.pdf", "dois.pdf", "tres.pdf"]]


def test_pro_acima_do_teto_de_arquivos_e_413(ctx):
    files = [("files", (f"{i}.pdf", _pdf(f"P{i}"), "application/pdf")) for i in range(6)]
    r = _post(ctx, PDF_PATH, files)
    assert r.status_code == 413, r.text
    assert "até 5 arquivos" in r.json()["detail"]
    assert ctx.state["extracted"] == []


def test_teto_de_bytes_vale_sobre_a_soma(ctx, monkeypatch):
    """Dois arquivos abaixo do teto cada um, acima dele juntos: 413. Sem a
    soma, N arquivos no limite custariam N× o que o plano pagou."""
    um = _pdf("A")
    monkeypatch.setitem(document_extract.MAX_DOCUMENT_BYTES, "Pro", int(len(um) * 1.5))
    r = _post(ctx, PDF_PATH, [("file", ("a.pdf", um, "application/pdf"))])
    assert r.status_code == 200, r.text
    r = _post(ctx, PDF_PATH, [
        ("files", ("a.pdf", um, "application/pdf")),
        ("files", ("b.pdf", um, "application/pdf")),
    ])
    assert r.status_code == 413, r.text
    assert "excede o limite deste plano" in r.json()["detail"]


def test_teto_de_paginas_vale_sobre_a_soma(ctx, monkeypatch):
    monkeypatch.setitem(document_extract.MAX_DOCUMENT_PAGES, "Pro", 3)
    r = _post(ctx, PDF_PATH, [
        ("files", ("a.pdf", _pdf("1", "2"), "application/pdf")),
        ("files", ("b.pdf", _pdf("3", "4"), "application/pdf")),
    ])
    assert r.status_code == 413, r.text
    assert "somam 4 páginas" in r.json()["detail"]


def test_documento_vazio_no_meio_aborta_nomeando_o_arquivo(ctx):
    r = _post(ctx, PDF_PATH, [
        ("files", ("ok.pdf", _pdf("TEXTO"), "application/pdf")),
        ("files", ("vazio.pdf", _pdf(""), "application/pdf")),
    ])
    assert r.status_code == 400, r.text
    assert '"vazio.pdf"' in r.json()["detail"]


def test_in_flight_e_liberado_com_varios_arquivos(ctx):
    _post(ctx, PDF_PATH, [
        ("files", ("a.pdf", _pdf("A"), "application/pdf")),
        ("files", ("b.pdf", _pdf("B"), "application/pdf")),
    ])
    assert sum(main.in_flight.values()) == 0


# ---------------------------------------------------------------------------
# imagens: mesma regra
# ---------------------------------------------------------------------------


def test_go_com_duas_imagens_e_413(ctx):
    ctx.state["plan"] = "Go"
    r = _post(ctx, IMG_PATH, [
        ("files", ("a.png", PNG, "image/png")),
        ("files", ("b.png", PNG, "image/png")),
    ])
    assert r.status_code == 413, r.text
    assert ctx.state["extracted"] == []


def test_pro_com_duas_imagens_devolve_pages_igual_ao_numero_de_imagens(ctx):
    r = _post(ctx, IMG_PATH, [
        ("files", ("a.png", PNG, "image/png")),
        ("files", ("b.png", PNG, "image/png")),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files"] == 2
    assert body["pages"] == 2
    assert body["ocr_used"] is True
    prompt = ctx.state["prompts"][0]
    assert "DOCUMENTO 1 DE 2 (a.png)" in prompt
    assert "TEXTO DE b.png" in prompt


def test_uma_imagem_continua_com_pages_1(ctx):
    r = _post(ctx, IMG_PATH, [("file", ("a.png", PNG, "image/png"))])
    assert r.status_code == 200, r.text
    assert r.json()["pages"] == 1
    assert r.json()["files"] == 1
