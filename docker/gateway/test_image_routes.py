"""Wiring HTTP das rotas /v1/images/* — o caminho que os módulos puros não cobrem.

    python3 -m pytest test_image_routes.py

image_gen e image_proxy testam DECISÕES (que path, que formato, que teto).
Aqui testa-se a LIGAÇÃO: o corpo chega ao pod do jeito certo, a resposta volta
com o contrato intacto, a imagem é gravada antes de responder, o in_flight é
sempre liberado e cada modo de falha vira o status certo.

É onde moram os bugs que nenhum dos dois lados vê sozinho — em especial os dois
que o desenho da persistência existe para evitar:

  * insert que dá timeout mas FOI aplicado: compensar ali apagaria os arquivos
    de uma linha viva;
  * falha de gravação virando 200: o cliente receberia a imagem achando que ela
    ficou guardada.

O authenticate/resolve_route reais ficam de fora (dependem de Supabase e de
estado de máquina): _authorize_image_request é substituído por um duplo que faz
o que o real faz de relevante aqui — devolver a rota e reservar o in_flight.
"""

import base64
import json
import os

import pytest

pytest.importorskip("fastapi", reason="wiring HTTP exige fastapi instalado")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

# main.py lê estas duas no IMPORT (não no lifespan) e levanta KeyError sem elas.
# Valores de fachada: nenhuma chamada real ao Supabase acontece aqui — o
# SupaClient é substituído pelo FakeSupa antes de qualquer teste rodar.
os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import image_gen  # noqa: E402
import image_proxy  # noqa: E402
import main  # noqa: E402

# A fixture `admissao` desliga o rate limit (os outros testes dela fariam
# rajadas e se atrapalhariam entre si). Guardado aqui, no import, para o teste
# que precisa do limite REAL poder religá-lo.
CHECK_RATE_LIMIT_REAL = main.check_rate_limit

PNG = b"\x89PNG\r\n\x1a\n" + b"conteudo da imagem"
PNG_B64 = base64.b64encode(PNG).decode()

MACHINE = {
    "id": "mach-1",
    "public_url": "https://pod.example",
    "served_model_name": None,
    "model_name": "black-forest-labs/FLUX.2-klein-4B",
}
ENTRY = {"api_key_id": "key-1", "account_id": "acc-1"}
STACK_ID = "stack-1"


class FakeSupa:
    """Duplo do SupaClient: guarda o que foi gravado, encena as falhas."""

    def __init__(self):
        self.uploaded: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.rows: list[dict] = []
        self.deleted: list[str] = []
        self.upload_error: Exception | None = None
        self.insert_error: Exception | None = None
        self.batch_exists_result = False
        self.batch_exists_error: Exception | None = None
        self.upload_attempts = 0

    async def upload_image_object(self, storage_path, data, content_type):
        self.upload_attempts += 1
        if self.upload_error:
            raise self.upload_error
        self.uploaded[storage_path] = data
        self.content_types[storage_path] = content_type

    async def insert_image_generations(self, rows):
        if self.insert_error:
            raise self.insert_error
        self.rows.extend(rows)

    async def image_batch_exists(self, batch_id):
        if self.batch_exists_error:
            raise self.batch_exists_error
        return self.batch_exists_result

    async def delete_image_objects(self, storage_paths):
        self.deleted.extend(storage_paths)
        for p in storage_paths:
            self.uploaded.pop(p, None)


@pytest.fixture
def ctx(monkeypatch):
    """Rotas de imagem prontas para chamar, com pod e Supabase falsos."""
    supa = FakeSupa()
    # raising=False: `supa` e `proxy_client` são só ANOTAÇÕES em main.py
    # (main.py:481) — o atributo passa a existir no lifespan, que estes
    # testes não rodam.
    monkeypatch.setattr(main, "supa", supa, raising=False)

    logged: list[dict] = []
    monkeypatch.setattr(
        main, "log_gateway_request", lambda **kw: logged.append(kw)
    )

    async def fake_authorize(authorization, request, path):
        flight_key = (STACK_ID, MACHINE["id"])
        main.in_flight[flight_key] += 1
        return ENTRY, "acc-1", MACHINE, STACK_ID, "Image"

    monkeypatch.setattr(main, "_authorize_image_request", fake_authorize)

    state = {
        "response": lambda request: httpx.Response(
            200, json={"created": 1, "data": [{"b64_json": PNG_B64}]}
        ),
        "seen": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        # força a leitura do corpo: é o que dispara o counting_stream do edits
        body = await request.aread()
        state["seen"].append((request, body))
        return state["response"](request)

    monkeypatch.setattr(
        main, "proxy_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        raising=False,
    )

    main.in_flight.clear()
    client = TestClient(main.app)
    yield type("Ctx", (), {
        "client": client, "supa": supa, "logged": logged, "state": state,
    })
    main.in_flight.clear()


def _post_generation(ctx, **body):
    payload = {"prompt": "um gato", **body}
    return ctx.client.post(
        "/v1/images/generations",
        json=payload,
        headers={"Authorization": "Bearer sk-teste"},
    )


def _flight_total():
    return sum(main.in_flight.values())


# ---------------------------------------------------------------------------
# caminho feliz
# ---------------------------------------------------------------------------


def test_contrato_do_cliente_continua_b64_json(ctx):
    r = _post_generation(ctx)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == [{"b64_json": PNG_B64}]


def test_imagem_e_gravada_antes_de_responder(ctx):
    r = _post_generation(ctx)
    assert r.status_code == 200
    # a resposta só sai depois de upload E insert: 200 significa "guardada"
    assert len(ctx.supa.uploaded) == 1
    assert list(ctx.supa.uploaded.values())[0] == PNG
    assert len(ctx.supa.rows) == 1


def test_linha_gravada_liga_a_imagem_a_quem_gerou(ctx):
    _post_generation(ctx)
    row = ctx.supa.rows[0]
    assert row["account_id"] == "acc-1"
    assert row["stack_id"] == STACK_ID
    assert row["api_key_id"] == "key-1"
    assert row["machine_id"] == "mach-1"
    assert row["path"] == "images/generations"


def test_objeto_sobe_com_o_content_type_real(ctx):
    # application/json (default do client REST) faria o navegador baixar
    _post_generation(ctx)
    assert list(ctx.supa.content_types.values()) == ["image/png"]


def test_storage_path_da_linha_aponta_para_o_objeto_gravado(ctx):
    _post_generation(ctx)
    assert ctx.supa.rows[0]["storage_path"] in ctx.supa.uploaded


def test_meta_do_pod_alimenta_a_linha(ctx):
    ctx.state["response"] = lambda request: httpx.Response(
        200,
        json={
            "created": 1,
            "data": [{"b64_json": PNG_B64}],
            "meta": {"prompt": "efetivo", "width": 1024, "height": 1024,
                     "steps": 4, "guidance_scale": 1.0, "seed": 123},
        },
    )
    _post_generation(ctx, prompt="pedido")
    row = ctx.supa.rows[0]
    assert row["prompt"] == "efetivo"
    assert row["seed"] == 123


def test_corpo_da_requisicao_alimenta_a_linha_quando_o_pod_nao_manda_meta(ctx):
    _post_generation(ctx, prompt="do cliente", size="1024x1536")
    row = ctx.supa.rows[0]
    assert row["prompt"] == "do cliente"
    assert (row["width"], row["height"]) == (1024, 1536)


def test_model_e_pinado_no_corpo_enviado_ao_pod(ctx):
    _post_generation(ctx, model="dall-e-3")
    _, body = ctx.state["seen"][-1]
    assert json.loads(body)["model"] == MACHINE["model_name"]


def test_requisicao_bem_sucedida_e_registrada(ctx):
    _post_generation(ctx)
    assert ctx.logged[-1]["status_code"] == 200
    assert ctx.logged[-1]["path"] == "images/generations"
    # difusão não produz tokens: null é a informação correta, zero seria contagem
    assert ctx.logged[-1]["usage"] is None


def test_in_flight_e_liberado_no_caminho_feliz(ctx):
    _post_generation(ctx)
    assert _flight_total() == 0


# ---------------------------------------------------------------------------
# falha de persistência: nunca vira 200
# ---------------------------------------------------------------------------


def test_falha_de_upload_vira_502_e_nao_200(ctx):
    # o pior bug possível aqui seria entregar a imagem dizendo que guardou
    ctx.supa.upload_error = RuntimeError("bucket fora do ar")
    r = _post_generation(ctx)
    assert r.status_code == 502
    assert "armazená-la" in r.json()["detail"]


def test_upload_tem_retry_curto(ctx):
    ctx.supa.upload_error = RuntimeError("soluço de rede")
    _post_generation(ctx)
    assert ctx.supa.upload_attempts == main.IMAGE_UPLOAD_ATTEMPTS


def test_falha_de_insert_vira_502(ctx):
    ctx.supa.insert_error = RuntimeError("postgrest fora do ar")
    r = _post_generation(ctx)
    assert r.status_code == 502


def test_resposta_ilegivel_do_pod_vira_502(ctx):
    ctx.state["response"] = lambda request: httpx.Response(200, json={"nada": "aqui"})
    r = _post_generation(ctx)
    assert r.status_code == 502
    assert "formato inesperado" in r.json()["detail"]


def test_in_flight_e_liberado_quando_a_persistencia_falha(ctx):
    ctx.supa.insert_error = RuntimeError("boom")
    _post_generation(ctx)
    assert _flight_total() == 0


def test_falha_de_persistencia_e_registrada_como_502(ctx):
    ctx.supa.insert_error = RuntimeError("boom")
    _post_generation(ctx)
    assert ctx.logged[-1]["status_code"] == 502


# ---------------------------------------------------------------------------
# compensação: o ponto delicado
# ---------------------------------------------------------------------------


def test_insert_que_falhou_de_verdade_apaga_os_arquivos(ctx):
    ctx.supa.insert_error = RuntimeError("postgrest fora do ar")
    ctx.supa.batch_exists_result = False  # confirmado: nada foi gravado
    _post_generation(ctx)
    assert ctx.supa.deleted, "arquivo órfão deveria ter sido removido"
    assert ctx.supa.uploaded == {}


def test_insert_que_deu_timeout_mas_foi_aplicado_preserva_os_arquivos(ctx):
    """O caso que justifica a verificação existir.

    Timeout não é prova de que a escrita não aconteceu — a resposta pode ter se
    perdido no retorno. Apagar aqui deixaria uma linha viva apontando para um
    objeto que não existe mais, que é pior que o órfão."""
    ctx.supa.insert_error = httpx.ReadTimeout("timeout na resposta")
    ctx.supa.batch_exists_result = True  # a linha está lá
    _post_generation(ctx)
    assert ctx.supa.deleted == []
    assert len(ctx.supa.uploaded) == 1


def test_verificacao_indisponivel_preserva_os_arquivos(ctx):
    # na dúvida não apaga: órfão custa MB, o inverso corrompe o histórico
    ctx.supa.insert_error = RuntimeError("boom")
    ctx.supa.batch_exists_error = RuntimeError("supabase inalcançável")
    _post_generation(ctx)
    assert ctx.supa.deleted == []


# ---------------------------------------------------------------------------
# repasse de status do pod
# ---------------------------------------------------------------------------


def test_429_de_fila_cheia_ganha_retry_after(ctx):
    """O pod manda o Retry-After dele, mas o agent remonta a resposta e não
    repassa headers do upstream — sem reconstruí-lo aqui, o cliente recebe um
    429 sem saber quando voltar."""
    ctx.state["response"] = lambda request: httpx.Response(
        429, json={"error": {"code": "queue_full"}}
    )
    r = _post_generation(ctx)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(main.IMAGE_QUEUE_RETRY_AFTER_S)


def test_erro_do_pod_nao_e_gravado_no_bucket(ctx):
    ctx.state["response"] = lambda request: httpx.Response(
        503, json={"error": {"code": "model_not_ready"}}
    )
    r = _post_generation(ctx)
    assert r.status_code == 503
    assert ctx.supa.uploaded == {}
    assert ctx.supa.rows == []


def test_erro_do_pod_libera_o_in_flight(ctx):
    ctx.state["response"] = lambda request: httpx.Response(500, json={"error": {}})
    _post_generation(ctx)
    assert _flight_total() == 0


def test_status_do_pod_e_repassado_ao_log(ctx):
    ctx.state["response"] = lambda request: httpx.Response(400, json={"error": {}})
    _post_generation(ctx)
    assert ctx.logged[-1]["status_code"] == 400


# ---------------------------------------------------------------------------
# tetos de corpo
# ---------------------------------------------------------------------------


def test_generations_com_corpo_gigante_e_413(ctx):
    grande = "x" * (image_proxy.max_generation_bytes() + 1)
    r = ctx.client.post(
        "/v1/images/generations",
        json={"prompt": grande},
        headers={"Authorization": "Bearer sk-teste"},
    )
    assert r.status_code == 413


def test_edits_repassa_o_multipart_com_o_boundary_intacto(ctx):
    r = ctx.client.post(
        "/v1/images/edits",
        files={"image": ("ref.png", PNG, "image/png")},
        data={"prompt": "deixe azul"},
        headers={"Authorization": "Bearer sk-teste"},
    )
    assert r.status_code == 200, r.text
    request, body = ctx.state["seen"][-1]
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert "boundary=" in request.headers["content-type"]
    assert PNG in body


def test_edits_grava_a_imagem_gerada(ctx):
    ctx.client.post(
        "/v1/images/edits",
        files={"image": ("ref.png", PNG, "image/png")},
        headers={"Authorization": "Bearer sk-teste"},
    )
    assert len(ctx.supa.rows) == 1
    assert ctx.supa.rows[0]["path"] == "images/edits"


def test_edits_sem_meta_do_pod_grava_prompt_nulo(ctx):
    # o multipart é repassado em streaming e nunca parseado pelo gateway
    ctx.client.post(
        "/v1/images/edits",
        files={"image": ("ref.png", PNG, "image/png")},
        data={"prompt": "invisível para o gateway"},
        headers={"Authorization": "Bearer sk-teste"},
    )
    assert ctx.supa.rows[0]["prompt"] is None


def test_edits_acima_do_teto_vira_413_e_nao_503(ctx, monkeypatch):
    """UploadTooLarge sobe embrulhado num erro de transporte; sem o unwrap, o
    cliente receberia 503 "máquina indisponível" para um corpo que ELE mandou
    grande demais."""
    monkeypatch.setattr(image_proxy, "max_edit_bytes", lambda: 64)
    r = ctx.client.post(
        "/v1/images/edits",
        files={"image": ("ref.png", b"z" * 4096, "image/png")},
        headers={"Authorization": "Bearer sk-teste"},
    )
    assert r.status_code == 413
    assert _flight_total() == 0


# ---------------------------------------------------------------------------
# Admissão: o que _authorize_image_request decide
# ---------------------------------------------------------------------------
#
# O fixture `ctx` acima substitui _authorize_image_request inteiro — é o certo
# para testar persistência, mas deixa de fora tudo que essa função decide. Aqui
# ela roda DE VERDADE, com só as dependências dela trocadas: é o que cobre o
# guard de plano, o toque de atividade e o teto de concorrência.


@pytest.fixture
def admissao(monkeypatch):
    """Rotas com _authorize_image_request REAL e só o I/O dele trocado."""
    supa = FakeSupa()
    monkeypatch.setattr(main, "supa", supa, raising=False)

    estado = {"plan": "Image", "logged": [], "tocado": [], "upstream": []}

    async def fake_authenticate(authorization, headers, path):
        if not authorization:
            raise main.HTTPException(status_code=401, detail="sem chave")
        return {
            "account_id": "acc-1",
            "api_key_id": "key-1",
            "stack_id": STACK_ID,
            "stacks": [{"id": STACK_ID, "plan": estado["plan"]}],
            "purpose": "customer",
        }, "hash-da-chave"

    async def fake_resolve_route(account_id, entry):
        return {**MACHINE, "max_concurrent_seqs": 4}, False, estado["plan"], STACK_ID

    async def fake_touch(stack_id, machine_id=None):
        estado["tocado"].append((stack_id, machine_id))

    async def fake_quota(account_id, plan, purpose="customer"):
        return None

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        estado["upstream"].append(request)
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": PNG_B64}]})

    monkeypatch.setattr(main, "authenticate", fake_authenticate)
    monkeypatch.setattr(main, "resolve_route", fake_resolve_route)
    monkeypatch.setattr(main, "maybe_touch", fake_touch)
    monkeypatch.setattr(main, "check_token_quota", fake_quota)
    monkeypatch.setattr(main, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(main, "log_gateway_request", lambda **kw: estado["logged"].append(kw))
    monkeypatch.setattr(
        main, "proxy_client",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        raising=False,
    )

    main.in_flight.clear()
    estado["client"] = TestClient(main.app)
    estado["supa"] = supa
    yield estado
    main.in_flight.clear()


def _gen(estado, **body):
    return estado["client"].post(
        "/v1/images/generations",
        json={"prompt": "um gato", **body},
        headers={"Authorization": "Bearer sk-teste"},
    )


@pytest.mark.parametrize("plano", ["Go", "Pro", "Max", "Enterprise"])
def test_chave_de_plano_de_llm_leva_403(admissao, plano):
    """Sem o guard, a requisição iria para o pod de vLLM da própria stack —
    cujo agent tem images/generations na allowlist, porque é o MESMO binário — e
    só morreria num 404 do vLLM, três camadas abaixo de onde dá pra explicar."""
    admissao["plan"] = plano
    r = _gen(admissao)
    assert r.status_code == 403
    assert "Image" in r.json()["detail"]
    assert admissao["upstream"] == []  # o pod nunca foi tocado
    assert _flight_total() == 0


def test_guard_de_plano_vale_para_edits(admissao):
    admissao["plan"] = "Pro"
    r = admissao["client"].post(
        "/v1/images/edits",
        headers={"Authorization": "Bearer sk-teste"},
        data={"prompt": "x"},
        files={"image": ("a.png", PNG, "image/png")},
    )
    assert r.status_code == 403
    assert _flight_total() == 0


def test_sem_chave_nao_chega_ao_pod(admissao):
    r = admissao["client"].post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 401
    assert admissao["upstream"] == []


def test_atividade_da_maquina_e_tocada(admissao):
    """last_activity_at é o que impede o idle-reaper de pausar uma máquina em
    uso — e é exatamente o que se perdia batendo direto na URL do pod."""
    _gen(admissao)
    assert admissao["tocado"] == [(STACK_ID, "mach-1")]


def test_concorrencia_acima_da_capacidade_vira_429(admissao):
    """check_concurrency roda de verdade: o excedente é recusado AQUI, com
    Retry-After, em vez de virar um queue_full do pod."""
    main.in_flight[(STACK_ID, "mach-1")] = 99
    r = _gen(admissao)
    assert r.status_code == 429
    assert admissao["upstream"] == []


def test_generations_nao_injeta_campos_de_chat(admissao):
    """O motivo de estas rotas NÃO estarem em ALLOWED_V1: pelo catch-all elas
    passariam por validate_body, que injeta max_tokens/stream_options e — ao ver
    o `prompt` string — tokenizaria o corpo contra um /tokenize que este pod não
    tem."""
    _gen(admissao)
    enviado = json.loads(admissao["upstream"][0].content)
    assert "max_tokens" not in enviado
    assert "stream_options" not in enviado
    assert set(enviado) == {"prompt", "model"}


def test_middleware_corta_generations_pelo_content_length(admissao, monkeypatch):
    """O guard do middleware cobre as DUAS rotas, não só o multipart: sem ele um
    Content-Length gigante num corpo JSON é lido inteiro pra RAM antes de o
    handler rodar."""
    monkeypatch.setenv("MAX_IMAGE_GENERATION_BYTES", "128")
    r = _gen(admissao, prompt="z" * 5000)
    assert r.status_code == 413
    assert admissao["upstream"] == []


def test_middleware_corta_edits_pelo_content_length(admissao, monkeypatch):
    """Caminho barato: cliente honesto declara o tamanho e o 413 sai antes de
    autenticar, sem ler byte nenhum do corpo."""
    monkeypatch.setenv("MAX_IMAGE_EDIT_BYTES", "1024")
    r = admissao["client"].post(
        "/v1/images/edits",
        headers={
            "Authorization": "Bearer sk-teste",
            "Content-Type": "multipart/form-data; boundary=abc",
        },
        content=b"y" * 5000,
    )
    assert r.status_code == 413
    assert admissao["upstream"] == []
    assert admissao["logged"] == []  # nem chegou a resolver rota


def test_pod_fora_do_ar_vira_503_e_libera_o_in_flight(admissao, monkeypatch):
    def caindo(request):
        raise httpx.ConnectError("pod fora do ar")

    monkeypatch.setattr(
        main, "proxy_client",
        httpx.AsyncClient(transport=httpx.MockTransport(caindo)),
        raising=False,
    )
    r = _gen(admissao)
    assert r.status_code == 503
    assert _flight_total() == 0
    assert admissao["logged"][-1]["status_code"] == 503


def test_falha_de_rede_no_edits_e_503_e_nao_413(admissao, monkeypatch):
    """Guarda contra o unwrap do UploadTooLarge ser largo demais: uma falha de
    rede GENUÍNA não pode virar 413 e mandar o cliente reduzir um corpo que
    nunca foi o problema."""
    def caindo(request):
        raise httpx.ConnectError("pod fora do ar")

    monkeypatch.setattr(
        main, "proxy_client",
        httpx.AsyncClient(transport=httpx.MockTransport(caindo)),
        raising=False,
    )
    r = admissao["client"].post(
        "/v1/images/edits",
        headers={"Authorization": "Bearer sk-teste"},
        data={"prompt": "x"},
        files={"image": ("a.png", PNG, "image/png")},
    )
    assert r.status_code == 503
    assert _flight_total() == 0


# ---------------------------------------------------------------------------
# Registro das rotas
# ---------------------------------------------------------------------------


def test_rotas_de_imagem_ficam_fora_do_allowed_v1():
    """Guarda executável do comentário no ALLOWED_V1. Adicioná-las ali as faria
    passar por validate_body — que é o que estes handlers existem para evitar."""
    assert "images/generations" not in main.ALLOWED_V1
    assert "images/edits" not in main.ALLOWED_V1


def test_rotas_de_imagem_vem_antes_do_catch_all():
    """O Starlette casa na ordem de registro. Se o catch-all vier primeiro, ele
    engole as duas — e, como elas não estão no ALLOWED_V1, devolve 404."""
    caminhos = [getattr(r, "path", None) for r in main.app.routes]
    catch_all = caminhos.index("/v1/{path:path}")
    assert caminhos.index("/v1/images/generations") < catch_all
    assert caminhos.index("/v1/images/edits") < catch_all


def test_corpo_200_nao_json_vira_erro_de_formato_e_nao_de_armazenamento(ctx):
    # a mensagem importa: "falha ao armazenar" mandaria o cliente investigar o
    # bucket quando o problema está na resposta do pod
    ctx.state["response"] = lambda request: httpx.Response(
        200, content=b"<html>nginx</html>", headers={"content-type": "text/html"}
    )
    r = _post_generation(ctx)
    assert r.status_code == 502
    assert "formato inesperado" in r.json()["detail"]


# ---------------------------------------------------------------------------
# rate limit: QUEM divide o teto
#
# A função em si é testada em test_rate_limit.py. O que se protege aqui é o
# call-site — que o handler passe a chave da STACK e não o hash da chave de API.
# Sem este teste, trocar um argumento pelo outro não quebraria nada visível: o
# limite continuaria funcionando, só que multiplicado por quantas chaves a stack
# tivesse emitido.
# ---------------------------------------------------------------------------


def _espiar_rate_limit(monkeypatch):
    vistos = []
    monkeypatch.setattr(
        main, "check_rate_limit", lambda bucket_key, plan: vistos.append((bucket_key, plan))
    )
    return vistos


def test_generations_limita_pela_stack_e_nao_pela_chave(admissao, monkeypatch):
    vistos = _espiar_rate_limit(monkeypatch)
    _gen(admissao)
    assert vistos == [(f"stack:{STACK_ID}", "Image")]


def test_edits_limita_pela_stack_e_nao_pela_chave(admissao, monkeypatch):
    vistos = _espiar_rate_limit(monkeypatch)
    admissao["client"].post(
        "/v1/images/edits",
        files={"image": ("ref.png", PNG, "image/png")},
        headers={"Authorization": "Bearer sk-teste"},
    )
    assert vistos == [(f"stack:{STACK_ID}", "Image")]


def test_o_bucket_usado_nao_e_o_hash_da_chave(admissao, monkeypatch):
    # "hash-da-chave" é o que fake_authenticate devolve como key_hash
    vistos = _espiar_rate_limit(monkeypatch)
    _gen(admissao)
    assert vistos[0][0] != "hash-da-chave"


def test_o_teto_do_image_e_de_4_por_rajada_ponta_a_ponta(admissao):
    """Com o check_rate_limit real: a 5ª requisição instantânea leva 429.

    É o burst de RATE_LIMIT_BURST["Image"] chegando ao cliente — alinhado à
    profundidade da fila do pod, não aos 12/min da vazão sustentada."""
    main.check_rate_limit = CHECK_RATE_LIMIT_REAL  # a fixture o havia desligado
    main.rate_buckets.clear()
    try:
        status = [_gen(admissao).status_code for _ in range(6)]
    finally:
        main.rate_buckets.clear()
    assert status[:4] == [200, 200, 200, 200]
    assert status[4] == 429
    assert status[5] == 429
