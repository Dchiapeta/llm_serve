"""Testes do wiring das rotas do server.py, sem GPU.

`torch`, `diffusers` e o carregamento dos pesos são substituídos por stubs em
sys.modules ANTES do import de `server` — o que este arquivo testa é a camada
HTTP (parse do corpo, multipart, mapeamento de erro para status), não inferência.
A geração é trocada por um worker falso.

Por que existe, além de test_policy.py: as regras puras já estão cobertas lá, mas
o que quebra em produção é o wiring — `image[]` não sendo lido, um
ImageRequestError levantado dentro do worker virando 500, ou o /health
respondendo 200 antes do warmup. Nada disso aparece testando funções puras, e
descobrir no boot de um pod A40 é caro.

Precisa de fastapi + python-multipart:

    <venv>/bin/python -m pytest docker/image/test_server_routes.py -q
"""

import io
import sys
import types

import pytest

pytest.importorskip("fastapi", reason="wiring HTTP exige fastapi instalado")
pytest.importorskip("multipart", reason="multipart exige python-multipart instalado")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs: torch e diffusers
# ---------------------------------------------------------------------------


def _install_stubs() -> None:
    torch = types.ModuleType("torch")
    torch.bfloat16 = "bfloat16"
    torch.float16 = "float16"
    torch.float32 = "float32"

    class _Generator:
        def __init__(self, device=None):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    torch.Generator = _Generator
    torch.inference_mode = lambda: _NullCtx()

    backends = types.SimpleNamespace(
        cuda=types.SimpleNamespace(matmul=types.SimpleNamespace(allow_tf32=False)),
        cudnn=types.SimpleNamespace(allow_tf32=False),
    )
    torch.backends = backends
    sys.modules["torch"] = torch

    diffusers = types.ModuleType("diffusers")

    class Flux2KleinPipeline:
        @classmethod
        def from_pretrained(cls, *a, **k):  # pragma: no cover - stub
            raise AssertionError("os testes não devem carregar pesos")

    diffusers.Flux2KleinPipeline = Flux2KleinPipeline
    sys.modules["diffusers"] = diffusers


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_install_stubs()

sys.path.insert(0, __import__("os").path.dirname(__file__))
import policy  # noqa: E402
import server  # noqa: E402


PNG_BYTES = None


def _png(size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = _png()


@pytest.fixture
def client(monkeypatch):
    """App com o modelo "pronto" e a geração trocada por um worker falso."""
    chamadas: list[server.GenPayload] = []

    def fake_run(payload: server.GenPayload):
        chamadas.append(payload)
        # decodifica de verdade: é assim que um byte inválido vira 400 e não 500
        for data in payload.references:
            server._decode_reference(data)
        return ["QUlP"] * payload.n

    monkeypatch.setattr(server, "READY", True)
    monkeypatch.setattr(server, "PIPE", object())
    monkeypatch.setattr(server, "LOAD_ERROR", None)
    queue = policy.GenerationQueue(fake_run, capacity=4, wait_timeout_s=5)
    monkeypatch.setattr(server, "QUEUE", queue)

    # lifespan real criaria a fila de novo e tentaria carregar pesos; aqui a
    # fila é a nossa e o worker é iniciado à mão.
    app = server.app
    original_lifespan = app.router.lifespan_context

    import contextlib

    @contextlib.asynccontextmanager
    async def noop_lifespan(_app):
        queue.start()
        try:
            yield
        finally:
            await queue.stop()

    app.router.lifespan_context = noop_lifespan
    with TestClient(app) as c:
        c.chamadas = chamadas  # type: ignore[attr-defined]
        yield c
    app.router.lifespan_context = original_lifespan


# ---------------------------------------------------------------------------
# health / models / metrics
# ---------------------------------------------------------------------------


def test_health_e_503_antes_do_warmup(monkeypatch):
    """O agent decide `vllm_ready` pelo STATUS CODE. Um 200 aqui marcaria a
    máquina como pronta no meio do download dos pesos, e o relógio de
    ociosidade começaria a correr durante o boot."""
    monkeypatch.setattr(server, "READY", False)
    monkeypatch.setattr(server, "LOAD_ERROR", None)

    import contextlib

    @contextlib.asynccontextmanager
    async def noop(_app):
        yield

    original = server.app.router.lifespan_context
    server.app.router.lifespan_context = noop
    try:
        with TestClient(server.app) as c:
            r = c.get("/health")
            assert r.status_code == 503
            assert r.json()["loading"] is True
    finally:
        server.app.router.lifespan_context = original


def test_health_expoe_o_erro_de_boot(monkeypatch):
    """Pod que falhou no load não pode ficar eternamente em "Subindo" sem
    ninguém saber por quê."""
    monkeypatch.setattr(server, "READY", False)
    monkeypatch.setattr(server, "LOAD_ERROR", "OSError: disco cheio")

    import contextlib

    @contextlib.asynccontextmanager
    async def noop(_app):
        yield

    original = server.app.router.lifespan_context
    server.app.router.lifespan_context = noop
    try:
        with TestClient(server.app) as c:
            r = c.get("/health")
            assert r.status_code == 503
            # o motivo vem prefixado com a CAUSA (boot / degradado / worker
            # parado), porque as três viram 503 e quem lê o /health precisa
            # distinguir "ainda subindo" de "subiu e quebrou"
            assert r.json()["error"] == "falha no boot: OSError: disco cheio"
            assert r.json()["loading"] is False
    finally:
        server.app.router.lifespan_context = original


def test_health_200_quando_pronto(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": server.SERVED_MODEL_NAME}


def test_models_lista_o_alias_servido(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == [server.SERVED_MODEL_NAME]


def test_metrics_responde_texto_para_o_admin_do_agent(client):
    """O /admin/vllm-metrics do agent faz GET /metrics e exige 200 com texto;
    sem esta rota aquele endpoint devolveria 502."""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "image_ready 1" in r.text
    assert "image_overlaps_total 0" in r.text


# ---------------------------------------------------------------------------
# generations
# ---------------------------------------------------------------------------


def test_generations_caminho_feliz(client):
    r = client.post(
        "/v1/images/generations",
        json={"model": server.SERVED_MODEL_NAME, "prompt": "um gato", "size": "1536x1024"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == [{"b64_json": "QUlP"}]
    assert "created" in body
    payload = client.chamadas[-1]
    assert (payload.width, payload.height) == (1536, 1024)
    assert payload.steps == server.STEPS
    assert payload.references == []


def test_generations_sem_prompt_e_400(client):
    r = client.post("/v1/images/generations", json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_prompt"


def test_generations_corpo_nao_json_e_400_e_nao_500(client):
    r = client.post(
        "/v1/images/generations",
        content=b"nao sou json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_body"


def test_generations_model_divergente_e_404(client):
    r = client.post("/v1/images/generations", json={"prompt": "x", "model": "dall-e-3"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_generations_response_format_url_e_400(client):
    r = client.post(
        "/v1/images/generations", json={"prompt": "x", "response_format": "url"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "response_format_not_supported"


def test_generations_size_fora_da_allowlist_e_400(client):
    r = client.post("/v1/images/generations", json={"prompt": "x", "size": "4096x4096"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_size"


def test_generations_com_image_aponta_para_a_rota_certa(client):
    """Sem isto, um cliente que mandasse referência no JSON receberia 200 com
    uma imagem gerada só do texto — silenciosamente diferente do que pediu."""
    r = client.post(
        "/v1/images/generations", json={"prompt": "x", "image": ["data:image/png;base64,AA"]}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "wrong_route_for_reference_image"


def test_generations_com_mask_e_400(client):
    r = client.post("/v1/images/generations", json={"prompt": "x", "mask": "AA"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "mask_not_supported"


def test_generations_n_acima_do_teto_e_400(client):
    r = client.post("/v1/images/generations", json={"prompt": "x", "n": 4})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_n"


def test_generations_503_quando_o_modelo_nao_esta_pronto(client, monkeypatch):
    monkeypatch.setattr(server, "READY", False)
    r = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "model_not_ready"


# ---------------------------------------------------------------------------
# edits (multipart)
# ---------------------------------------------------------------------------


def test_edits_aceita_o_campo_image(client):
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "deixa noturno"},
        files={"image": ("a.png", PNG_BYTES, "image/png")},
    )
    assert r.status_code == 200, r.text
    assert len(client.chamadas[-1].references) == 1


def test_edits_aceita_o_campo_image_bracket_da_openai(client):
    """Os exemplos oficiais da OpenAI usam `image[]` para múltiplas imagens.
    `image[]` não é identificador Python válido, então o form é lido à mão —
    este teste é o que prova que a leitura manual funciona."""
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "junta as duas"},
        files=[
            ("image[]", ("a.png", PNG_BYTES, "image/png")),
            ("image[]", ("b.png", PNG_BYTES, "image/png")),
        ],
    )
    assert r.status_code == 200, r.text
    assert len(client.chamadas[-1].references) == 2


def test_edits_mistura_image_e_image_bracket_na_mesma_lista(client):
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files=[
            ("image", ("a.png", PNG_BYTES, "image/png")),
            ("image[]", ("b.png", PNG_BYTES, "image/png")),
        ],
    )
    assert r.status_code == 200, r.text
    assert len(client.chamadas[-1].references) == 2


def test_edits_sem_imagem_e_400(client):
    r = client.post("/v1/images/edits", data={"prompt": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_image"


def test_edits_acima_do_teto_de_referencias_e_400(client):
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files=[("image[]", (f"{i}.png", PNG_BYTES, "image/png")) for i in range(5)],
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "too_many_reference_images"


def test_edits_com_mask_e_400_nunca_ignorada(client):
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files=[
            ("image", ("a.png", PNG_BYTES, "image/png")),
            ("mask", ("m.png", PNG_BYTES, "image/png")),
        ],
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "mask_not_supported"


def test_edits_formato_vem_dos_magic_bytes_nao_do_content_type(client):
    """Content-type e nome do arquivo são declarados pelo cliente."""
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files={"image": ("a.png", b"GIF89a" + b"\x00" * 64, "image/png")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_image_format"


def test_edits_imagem_grande_e_413(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_FILE_SIZE_BYTES", 1024)
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files={"image": ("a.png", PNG_BYTES + b"\x00" * 4096, "image/png")},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "image_too_large"


def test_edits_campo_image_como_texto_e_400(client):
    r = client.post("/v1/images/edits", data={"prompt": "x", "image": "nao sou arquivo"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_image_field"


def test_erro_levantado_dentro_do_worker_vira_400_e_nao_500(client):
    """Imagem com magic byte válido mas conteúdo corrompido só falha na
    decodificação, que acontece na thread do worker. Sem o exception_handler
    pegando ImageRequestError vindo do Future, isso seria 500."""
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files={"image": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\xff" * 64, "image/png")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "undecodable_image"


# ---------------------------------------------------------------------------
# fila
# ---------------------------------------------------------------------------


def test_fila_cheia_devolve_429_com_retry_after(client, monkeypatch):
    class _Cheia:
        completed = 0
        depth = 0
        in_flight = 0
        overlaps = 0
        # _unhealthy_reason consulta os dois: uma fila sem `alive` faria o
        # /health e o _dispatch estourarem AttributeError -> 500
        alive = True
        worker_error = None

        async def submit(self, _payload):
            raise policy.QueueFull("fila de geração cheia (4 em voo)")

    monkeypatch.setattr(server, "QUEUE", _Cheia())
    r = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "5"
    assert r.json()["error"]["code"] == "queue_full"


def test_timeout_de_fila_devolve_504(client, monkeypatch):
    class _Lenta:
        completed = 0
        depth = 0
        in_flight = 0
        overlaps = 0
        alive = True
        worker_error = None

        async def submit(self, _payload):
            raise policy.QueueWaitTimeout("espera na fila excedeu 60s")

    monkeypatch.setattr(server, "QUEUE", _Lenta())
    r = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "queue_timeout"


# ---------------------------------------------------------------------------
# Tipos errados chegam como 400, nunca 500 (regressão medida: 13 casos em 500)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": "x", "size": 1024},
        {"prompt": "x", "size": ["1024x1024"]},
        {"prompt": "x", "steps": "abc"},
        {"prompt": "x", "steps": {}},
        {"prompt": "x", "steps": 7.9},
        {"prompt": "x", "n": "abc"},
        {"prompt": "x", "n": True},
        {"prompt": "x", "guidance_scale": "alto"},
        {"prompt": "x", "seed": "abc"},
        {"prompt": "x", "seed": []},
        {"prompt": "x", "model": 1024},
        {"prompt": "x", "response_format": []},
    ],
)
def test_json_com_tipo_errado_e_400(client, body):
    r = client.post("/v1/images/generations", json=body)
    assert r.status_code == 400, f"{body} -> {r.status_code} {r.text}"
    assert r.json()["error"]["code"], "erro sem code não é acionável por máquina"


@pytest.mark.parametrize("campo", ["size", "steps", "n", "guidance_scale", "seed"])
def test_multipart_com_campo_escalar_enviado_como_arquivo_e_400(client, campo):
    """form.get() devolve UploadFile quando o cliente manda o campo como
    ARQUIVO. Antes da correção, os cinco davam 500."""
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files=[
            ("image", ("a.png", PNG_BYTES, "image/png")),
            (campo, ("v.txt", b"1024x1024", "text/plain")),
        ],
    )
    assert r.status_code == 400, f"{campo} -> {r.status_code} {r.text}"


def test_multipart_aceita_escalares_como_texto(client):
    """A correção não pode ter quebrado o caminho normal: multipart entrega
    tudo como string."""
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x", "steps": "4", "n": "1", "guidance_scale": "1.0", "seed": "42"},
        files={"image": ("a.png", PNG_BYTES, "image/png")},
    )
    assert r.status_code == 200, r.text
    p = client.chamadas[-1]
    assert (p.steps, p.n, p.guidance_scale, p.seed) == (4, 1, 1.0, 42)


def test_model_com_o_path_do_hf_e_aceito(client, monkeypatch):
    """O pin_model do gateway sobrescreve `model` com machines.model_name (o
    path do HF), porque este template não tem --served-model-name para o
    vllmFlagsFromTemplate extrair. Sem MODEL_ALIASES seria 404 em 100% das
    requests quando images/* entrar no ALLOWED_V1 do gateway."""
    hf = "black-forest-labs/FLUX.2-klein-4B"
    monkeypatch.setattr(server, "MODEL_ALIASES", frozenset({hf}))
    r = client.post("/v1/images/generations", json={"prompt": "x", "model": hf})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Saúde: degradação pós-boot e worker morto
# ---------------------------------------------------------------------------


def test_health_503_quando_degradado(client, monkeypatch):
    """READY=True só diz que o boot passou. Um CUDA illegal memory access
    depois disso deixa o pipeline inutilizável com o processo VIVO — e aí
    /health 200 significa máquina saudável no painel e erro em toda geração."""
    monkeypatch.setattr(server, "DEGRADED", "3 falhas consecutivas")
    r = client.get("/health")
    assert r.status_code == 503
    assert "degradado" in r.json()["error"]


def test_health_503_quando_o_worker_morreu(client, monkeypatch):
    """Worker morto = 504 em toda request, indefinidamente, com /health 200:
    máquina saudável no painel e erro em tudo. O /health tem que denunciar.

    Substitui a fila por um stub morto em vez de cancelar a task real — a task
    do worker vive no event loop do TestClient, e cancelá-la de fora dá
    "attached to a different loop"."""

    class _Morta:
        completed = depth = in_flight = overlaps = 0
        alive = False
        worker_error = "RuntimeError: morri"

        async def submit(self, _p):  # pragma: no cover - nunca chamado
            raise AssertionError("não deveria aceitar request com worker morto")

    monkeypatch.setattr(server, "QUEUE", _Morta())
    r = client.get("/health")
    assert r.status_code == 503
    assert "worker" in r.json()["error"]
    assert "morri" in r.json()["error"]

    # e não aceita geração
    g = client.post("/v1/images/generations", json={"prompt": "x"})
    assert g.status_code == 503
    assert g.json()["error"]["code"] == "model_not_ready"


def test_geracao_recusada_quando_degradado(client, monkeypatch):
    monkeypatch.setattr(server, "DEGRADED", "pipeline quebrado")
    r = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "model_not_ready"


def test_falhas_consecutivas_degradam_o_pod(client, monkeypatch):
    """Erro de CLIENTE não conta; falha de geração conta."""
    monkeypatch.setattr(server, "DEGRADED", None)
    monkeypatch.setattr(server, "CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(server, "DEGRADED_AFTER_FAILURES", 2)

    class _Explode:
        completed = depth = in_flight = overlaps = 0
        alive = True
        worker_error = None

        async def submit(self, _p):
            raise RuntimeError("CUDA error: illegal memory access")

    monkeypatch.setattr(server, "QUEUE", _Explode())
    r1 = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r1.status_code == 500
    assert r1.json()["error"]["code"] == "generation_failed"
    assert server.DEGRADED is None  # 1 falha ainda não degrada

    r2 = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r2.status_code == 500
    assert server.DEGRADED is not None  # 2 falhas: degradado

    # e agora o /health denuncia
    assert client.get("/health").status_code == 503


def test_erro_de_cliente_no_worker_nao_degrada(client, monkeypatch):
    monkeypatch.setattr(server, "DEGRADED", None)
    monkeypatch.setattr(server, "CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(server, "DEGRADED_AFTER_FAILURES", 1)
    for _ in range(3):
        r = client.post(
            "/v1/images/edits",
            data={"prompt": "x"},
            files={"image": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\xff" * 64, "image/png")},
        )
        assert r.status_code == 400
    assert server.DEGRADED is None, "imagem corrompida não é sintoma de GPU quebrada"


def test_worker_stopped_vira_503(client, monkeypatch):
    class _Parado:
        completed = depth = in_flight = overlaps = 0
        alive = True
        worker_error = None

        async def submit(self, _p):
            raise policy.WorkerStopped("worker interrompido durante a geração")

    monkeypatch.setattr(server, "QUEUE", _Parado())
    r = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "worker_stopped"


def test_metrics_expoe_worker_e_degradacao(client):
    r = client.get("/metrics")
    assert "image_worker_alive 1" in r.text
    assert "image_degraded 0" in r.text
    assert "image_consecutive_failures 0" in r.text


# ---------------------------------------------------------------------------
# multipart: teto imposto no parser
# ---------------------------------------------------------------------------


def test_excesso_de_arquivos_e_400_e_nao_500(client):
    """max_files no parser aborta antes de materializar tudo; o erro do
    Starlette não é ImageRequestError e escaparia como 500 sem o wrap."""
    r = client.post(
        "/v1/images/edits",
        data={"prompt": "x"},
        files=[("image[]", (f"{i}.png", PNG_BYTES, "image/png")) for i in range(12)],
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] in ("too_many_reference_images", "invalid_multipart")


# ---------------------------------------------------------------------------
# meta: parâmetros efetivos da geração
#
# Existe para quem PERSISTE a imagem (o gateway grava cada geração no bucket e
# precisa saber com que parâmetros ela saiu). Dois casos que o gateway não tem
# como resolver sozinho: a seed sorteada, e o /v1/images/edits inteiro — lá o
# corpo é multipart repassado em streaming e nunca chega a ser parseado por ele.
# ---------------------------------------------------------------------------


def test_generations_devolve_meta_com_os_parametros_efetivos(client):
    r = client.post(
        "/v1/images/generations",
        json={"prompt": "um gato", "size": "1536x1024", "steps": 6, "seed": 42},
    )
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["prompt"] == "um gato"
    assert (meta["width"], meta["height"]) == (1536, 1024)
    assert meta["steps"] == 6
    assert meta["seed"] == 42
    assert meta["model"] == server.SERVED_MODEL_NAME


def test_meta_traz_a_seed_sorteada_quando_o_cliente_nao_manda(client):
    # sem isto a imagem seria irreproduzível: nem o cliente nem nós saberíamos
    # com que seed ela saiu
    r = client.post("/v1/images/generations", json={"prompt": "um gato"})
    assert r.status_code == 200, r.text
    seed = r.json()["meta"]["seed"]
    assert isinstance(seed, int)
    assert 0 <= seed <= policy.SEED_MAX
    # e é a MESMA que foi para o pipeline
    assert client.chamadas[-1].seed == seed


def test_seed_sorteada_muda_entre_requisicoes(client):
    seeds = set()
    for _ in range(5):
        r = client.post("/v1/images/generations", json={"prompt": "x"})
        seeds.add(r.json()["meta"]["seed"])
    assert len(seeds) > 1, "seed sorteada não deveria repetir sempre"


def test_meta_nao_desloca_o_contrato_de_data(client):
    # `meta` é campo EXTRA: cliente OpenAI ignora desconhecidos, mas `data`
    # precisa continuar exatamente como estava
    r = client.post("/v1/images/generations", json={"prompt": "um gato"})
    assert r.json()["data"] == [{"b64_json": "QUlP"}]
