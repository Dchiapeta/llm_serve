"""Servidor de geração de imagem que roda no pod, no lugar do vLLM.

Ocupa exatamente o mesmo lugar na arquitetura que o `vllm.entrypoints.openai.api_server`
ocupa nos pods de LLM: escuta só em 127.0.0.1:8001, e quem fala com o mundo é o
agent na 8000 (docker/agent/main.py), que autentica a chave, contabiliza uso e
proxia `/v1/*`.

    cliente → gateway → agent do pod (:8000) → ESTE servidor (:8001)

Consequências desse lugar, todas respeitadas abaixo:

  * `GET /health` só responde 200 quando o modelo está REALMENTE pronto (pesos
    carregados + warmup feito). O agent traduz isso em `vllm_ready`, e o
    reconciliador do painel/gateway usa `vllm_ready` para promover a máquina a
    "running". Responder 200 cedo faz o relógio de ociosidade começar durante o
    boot e a auto-pausa mata a máquina antes de ela servir alguém.
  * `GET /metrics` existe porque o `/admin/vllm-metrics` do agent bate nele; sem
    a rota, aquele endpoint administrativo devolveria 502.
  * O event loop NUNCA roda inferência. Toda geração vai para
    asyncio.to_thread pela GenerationQueue (policy.py) — se travasse aqui, o
    /health pararia de responder junto e o pod seria marcado como falho no meio
    de uma geração normal.

Toda regra testável sem GPU vive em policy.py; aqui fica só a parte suja (torch,
diffusers, PIL, FastAPI).
"""

import asyncio
import base64
import contextlib
import io
import os
import time
from dataclasses import dataclass, field

import torch
from diffusers import Flux2KleinPipeline
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from PIL import Image

import policy

# ---------------------------------------------------------------------------
# Configuração (env do template — ver scripts/_tmp-create-image-template.mjs)
# ---------------------------------------------------------------------------

# MODEL_NAME é injetada por máquina pelo painel (lib/actions.ts:podInputFromTemplate),
# igual nos pods de vLLM — não fica no env do template.
MODEL_NAME = os.environ.get("MODEL_NAME", "")
# Commit do HF. Sem isto, from_pretrained segue o `main` do repo e o pod deixa
# de ser reproduzível no dia em que a Black Forest Labs republicar os pesos.
MODEL_REVISION = os.environ.get("IMAGE_MODEL_REVISION") or None
SERVED_MODEL_NAME = os.environ.get("IMAGE_SERVED_MODEL_NAME", "flux2-klein-4b")
# Nomes ADICIONAIS aceitos no campo `model`, além do alias servido.
#
# O path do HF entra aqui por causa do gateway: `machines.served_model_name` é
# extraído de VLLM_EXTRA_ARGS (lib/machines.ts:vllmFlagsFromTemplate), e este
# template não tem essa env — então a coluna fica NULL e o `effective_model_name`
# do gateway cai no fallback `machines.model_name`, que é o path do HF. O
# `pin_model` SOBRESCREVE o campo `model` do cliente com esse valor. Aceitando
# só o alias, 100% das requests viriam com 404 model_not_found no dia em que
# `images/*` entrar no ALLOWED_V1 do gateway.
MODEL_ALIASES = frozenset(n for n in (MODEL_NAME,) if n)

_DTYPES = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}
_DTYPE_NAME = os.environ.get("IMAGE_DTYPE", "bfloat16")
if _DTYPE_NAME not in _DTYPES:
    # SystemExit com mensagem, e não KeyError cru: um typo em IMAGE_DTYPE mata o
    # processo no IMPORT, antes de o _boot rodar — então LOAD_ERROR fica None e o
    # /health não tem o que reportar. A única pista seria o traceback no
    # /admin/logs, e um KeyError: 'bf16' não diz o que fazer.
    raise SystemExit(
        f"[server] IMAGE_DTYPE inválida: {_DTYPE_NAME!r}. Aceitas: {', '.join(_DTYPES)}"
    )
DTYPE = _DTYPES[_DTYPE_NAME]

STEPS = int(os.environ.get("IMAGE_STEPS", "4"))
STEPS_MAX = int(os.environ.get("IMAGE_STEPS_MAX", "8"))
GUIDANCE_SCALE = float(os.environ.get("IMAGE_GUIDANCE_SCALE", "1.0"))
MAX_SEQUENCE_LENGTH = int(os.environ.get("IMAGE_MAX_SEQUENCE_LENGTH", "512"))
DEFAULT_SIZE = os.environ.get("IMAGE_DEFAULT_SIZE", "1024x1024")
ALLOWED_SIZES = policy.parse_size_list(
    os.environ.get("IMAGE_ALLOWED_SIZES", "1024x1024,1536x1024,1024x1536")
)
IMAGES_PER_REQUEST_MAX = int(os.environ.get("IMAGE_IMAGES_PER_REQUEST_MAX", "1"))
OUTPUT_FORMAT = os.environ.get("IMAGE_OUTPUT_FORMAT", "png").upper()

MAX_REFERENCE_IMAGES = int(os.environ.get("IMAGE_MAX_REFERENCE_IMAGES", "4"))
ALLOWED_FORMATS = frozenset(
    f.strip().lower()
    for f in os.environ.get("IMAGE_ALLOWED_FORMATS", "png,jpeg,webp").split(",")
    if f.strip()
)
MAX_FILE_SIZE_BYTES = int(os.environ.get("IMAGE_MAX_FILE_SIZE_MB", "15")) * 1024 * 1024

QUEUE_CAPACITY = int(os.environ.get("IMAGE_QUEUE_CAPACITY", "4"))
QUEUE_WAIT_TIMEOUT_S = float(os.environ.get("IMAGE_QUEUE_WAIT_TIMEOUT_S", "60"))
WARMUP_RUNS = int(os.environ.get("IMAGE_WARMUP_RUNS", "2"))
ALLOW_TF32 = os.environ.get("IMAGE_ALLOW_TF32", "true").lower() == "true"

DEVICE = os.environ.get("IMAGE_DEVICE", "cuda")

GENERATIONS_PATH = "images/generations"
EDITS_PATH = "images/edits"


def log(msg: str) -> None:
    # stdout do processo; o entrypoint prefixa com [image] e faz tee para o
    # arquivo que o /admin/logs do agent lê.
    print(f"[server] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Estado do processo
# ---------------------------------------------------------------------------

PIPE: Flux2KleinPipeline | None = None
READY = False
LOAD_ERROR: str | None = None
QUEUE: policy.GenerationQueue | None = None

# Falha PÓS-boot. READY=True só diz que o load e o warmup passaram; um
# `CUDA illegal memory access` depois disso deixa o pipeline inutilizável com o
# processo VIVO — e aí /health continua 200, o reconciliador mantém a máquina em
# "running", o reaper a conta como em uso, e toda geração devolve erro. Nos pods
# de vLLM esse cenário mata o processo e o vllm_alive=false denuncia; aqui não.
# Precedente: o MTP do INT4 derrubando o engine com 2 requests concorrentes.
#
# Conta apenas falhas CONSECUTIVAS e não-de-cliente: uma imagem corrompida
# (ImageRequestError) não é sintoma de GPU quebrada, e uma falha isolada pode
# ser transitória.
DEGRADED: str | None = None
CONSECUTIVE_FAILURES = 0
DEGRADED_AFTER_FAILURES = int(os.environ.get("IMAGE_DEGRADED_AFTER_FAILURES", "3"))


@dataclass
class GenPayload:
    prompt: str
    width: int
    height: int
    steps: int
    guidance_scale: float
    n: int
    # sempre um inteiro desde que as rotas passaram a resolver a seed com
    # policy.ensure_seed: nunca mais é None. É o que permite ao `meta` da
    # resposta dizer com que seed a imagem saiu.
    seed: int
    # bytes crus; a decodificação acontece na thread do worker, não no event
    # loop — 4 referências de 15 MB são trabalho de CPU suficiente para
    # travar o /health se fosse feito aqui.
    references: list[bytes] = field(default_factory=list)


def _decode_reference(data: bytes) -> Image.Image:
    """bytes -> PIL RGB, sem metadata.

    `convert("RGB")` derruba canal alpha e perfis de cor, e o reencode implícito
    na próxima etapa descarta EXIF — que é o `strip_metadata` da configuração.
    Um EXIF com geolocalização atravessando o pipeline e voltando embutido na
    saída seria vazamento de dado do usuário.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise policy.ImageRequestError(
            f"imagem de referência não pôde ser decodificada: {e}",
            code="undecodable_image",
        ) from None
    return img.convert("RGB")


def _encode_output(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format=OUTPUT_FORMAT)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _run(payload: GenPayload) -> list[str]:
    """Geração. SÍNCRONA e sempre executada em to_thread pela GenerationQueue."""
    assert PIPE is not None
    references = [_decode_reference(d) for d in payload.references]

    # sempre com generator explícito: a seed já vem resolvida (ensure_seed), e é
    # ela que a resposta promete no `meta`. Deixar o torch sortear internamente
    # tornaria essa promessa falsa.
    generator = torch.Generator(device=DEVICE).manual_seed(payload.seed)

    with torch.inference_mode():
        out = PIPE(
            prompt=payload.prompt,
            # None e não [] quando não há referência: uma lista vazia faz o
            # pipeline entrar no caminho de image-to-image sem imagem.
            image=references or None,
            width=payload.width,
            height=payload.height,
            num_inference_steps=payload.steps,
            guidance_scale=payload.guidance_scale,
            num_images_per_prompt=payload.n,
            max_sequence_length=MAX_SEQUENCE_LENGTH,
            generator=generator,
            output_type="pil",
        )
    return [_encode_output(img) for img in out.images]


def _load_pipeline() -> Flux2KleinPipeline:
    log(f"carregando {MODEL_NAME} (revision={MODEL_REVISION or 'main'}, dtype={DTYPE})")
    # `dtype=` e não `torch_dtype=`: na 0.40 o segundo emite FutureWarning
    # ("será removido na 1.0.0"). Visto no boot do container de smoke test.
    pipe = Flux2KleinPipeline.from_pretrained(
        MODEL_NAME, revision=MODEL_REVISION, dtype=DTYPE
    )
    # .to(device) e não enable_model_cpu_offload(): o offload economiza VRAM
    # trocando pesos entre CPU e GPU a cada passo, e numa A40 de 48 GB com ~16 GB
    # de pesos não há o que economizar — só custaria latência.
    pipe.to(DEVICE)
    pipe.set_progress_bar_config(disable=True)
    return pipe


async def _boot() -> None:
    """Carrega os pesos e faz o warmup. Roda em background durante o lifespan.

    Fora do caminho do `yield` de propósito: o uvicorn só começa a aceitar
    conexões depois que o lifespan cede, e o /health precisa estar respondendo
    (503) durante os minutos de download. Se o load fosse feito antes do yield,
    o painel veria connection refused em vez de "Subindo".
    """
    global PIPE, READY, LOAD_ERROR
    started = time.monotonic()
    try:
        if ALLOW_TF32:
            # TF32 no matmul: mesma qualidade visual, matmul mais rápido em
            # Ampere. A A40 é SM 8.6, então vale.
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True

        PIPE = await asyncio.to_thread(_load_pipeline)
        log(f"pesos carregados em {time.monotonic() - started:.1f}s")

        width, height = policy.validate_size(
            DEFAULT_SIZE, default=DEFAULT_SIZE, allowed=ALLOWED_SIZES
        )
        for i in range(WARMUP_RUNS):
            t0 = time.monotonic()
            await asyncio.to_thread(
                _run,
                GenPayload(
                    prompt="warmup",
                    width=width,
                    height=height,
                    steps=STEPS,
                    guidance_scale=GUIDANCE_SCALE,
                    n=1,
                    seed=0,
                ),
            )
            log(f"warmup {i + 1}/{WARMUP_RUNS} em {time.monotonic() - t0:.1f}s")

        READY = True
        log(f"pronto para servir '{SERVED_MODEL_NAME}' (boot total {time.monotonic() - started:.1f}s)")
    except Exception as e:
        # Não engolir: o /health passa a reportar o erro, e o /admin/logs do
        # agent mostra o traceback. Silenciar aqui deixaria o pod eternamente
        # em "Subindo" sem ninguém saber por quê.
        LOAD_ERROR = f"{type(e).__name__}: {e}"
        log(f"FALHA no boot: {LOAD_ERROR}")
        raise


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global QUEUE
    QUEUE = policy.GenerationQueue(
        _run, capacity=QUEUE_CAPACITY, wait_timeout_s=QUEUE_WAIT_TIMEOUT_S
    )
    QUEUE.start()
    boot = asyncio.create_task(_boot(), name="image-boot")
    try:
        yield
    finally:
        boot.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await boot
        await QUEUE.stop()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(policy.ImageRequestError)
async def _on_request_error(_request: Request, exc: policy.ImageRequestError):
    # Formato de erro da OpenAI: um SDK já sabe ler `error.message` e
    # `error.code`.
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": "invalid_request_error",
                "code": exc.code,
            }
        },
    )


# ---------------------------------------------------------------------------
# Health e metrics
# ---------------------------------------------------------------------------


def _unhealthy_reason() -> str | None:
    """Motivo de o pod não estar apto a servir, ou None se está.

    Três causas distintas, e todas TÊM que virar 503 — o agent traduz o status
    code em `vllm_ready`, e é `vllm_ready` que o reconciliador usa para manter a
    máquina em "running" e o reaper para contá-la como em uso. Qualquer uma
    delas respondendo 200 produz o pior estado possível: máquina saudável no
    painel, erro em toda geração.
    """
    if LOAD_ERROR is not None:
        return f"falha no boot: {LOAD_ERROR}"
    if not READY:
        return None  # carregando: não é falha, mas também não é apto
    if DEGRADED is not None:
        return f"degradado: {DEGRADED}"
    if QUEUE is None or not QUEUE.alive:
        # Worker morto = nenhuma geração possível. Sem esta checagem o pod
        # respondia 200 e devolvia 504 em toda request, indefinidamente.
        return f"worker de geração parado: {QUEUE.worker_error if QUEUE else 'sem fila'}"
    return None


@app.get("/health")
async def health():
    reason = _unhealthy_reason()
    if READY and reason is None:
        return {"ok": True, "model": SERVED_MODEL_NAME}
    # 503 e não 200-com-flag: o agent decide `vllm_ready` pelo STATUS CODE
    # (docker/agent/main.py:health faz `r.status_code == 200`). Um 200 aqui
    # marcaria a máquina como pronta no meio do download dos pesos.
    return JSONResponse(
        status_code=503,
        content={"ok": False, "loading": reason is None, "error": reason},
    )


@app.get("/metrics")
async def metrics():
    q = QUEUE
    lines = [
        "# HELP image_ready 1 quando os pesos estão carregados e o warmup terminou",
        "# TYPE image_ready gauge",
        f"image_ready {1 if READY else 0}",
        "# HELP image_generations_total Gerações concluídas desde o boot",
        "# TYPE image_generations_total counter",
        f"image_generations_total {q.completed if q else 0}",
        "# HELP image_queue_depth Jobs aguardando o worker",
        "# TYPE image_queue_depth gauge",
        f"image_queue_depth {q.depth if q else 0}",
        "# HELP image_in_flight Requisições aceitas e não respondidas",
        "# TYPE image_in_flight gauge",
        f"image_in_flight {q.in_flight if q else 0}",
        # Sempre 0 por construção (um consumidor só). Se algum dia não for, é
        # quebra de invariante, e a métrica é o que denuncia.
        "# HELP image_overlaps_total Gerações que se sobrepuseram (invariante: 0)",
        "# TYPE image_overlaps_total counter",
        f"image_overlaps_total {q.overlaps if q else 0}",
        "# HELP image_worker_alive 1 quando a task consumidora está rodando",
        "# TYPE image_worker_alive gauge",
        f"image_worker_alive {1 if (q and q.alive) else 0}",
        "# HELP image_degraded 1 quando o pipeline falhou N vezes consecutivas",
        "# TYPE image_degraded gauge",
        f"image_degraded {1 if DEGRADED else 0}",
        "# HELP image_consecutive_failures Falhas de geração seguidas (zera no sucesso)",
        "# TYPE image_consecutive_failures gauge",
        f"image_consecutive_failures {CONSECUTIVE_FAILURES}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": SERVED_MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "trystac",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Geração
# ---------------------------------------------------------------------------


def _note_failure(e: Exception) -> None:
    """Conta uma falha de GERAÇÃO (não de cliente) e degrada o pod se insistir."""
    global CONSECUTIVE_FAILURES, DEGRADED
    CONSECUTIVE_FAILURES += 1
    log(f"falha de geração #{CONSECUTIVE_FAILURES}: {type(e).__name__}: {e}")
    if CONSECUTIVE_FAILURES >= DEGRADED_AFTER_FAILURES and DEGRADED is None:
        DEGRADED = f"{CONSECUTIVE_FAILURES} falhas consecutivas; última: {type(e).__name__}: {e}"
        log(f"DEGRADADO — /health passa a responder 503. {DEGRADED}")


def _note_success() -> None:
    global CONSECUTIVE_FAILURES
    CONSECUTIVE_FAILURES = 0


async def _dispatch(payload: GenPayload) -> JSONResponse:
    reason = _unhealthy_reason()
    if not READY or QUEUE is None or reason is not None:
        raise policy.ImageRequestError(
            reason or "modelo ainda carregando",
            status_code=503,
            code="model_not_ready",
        )
    try:
        images = await QUEUE.submit(payload)
    except policy.QueueFull as e:
        return JSONResponse(
            status_code=429,
            content={"error": {"message": str(e), "type": "rate_limit_error", "code": "queue_full"}},
            # O agent NÃO repassa headers do upstream (monta uma JSONResponse
            # nova), então este Retry-After só chega a quem bate direto no pod.
            # Fica porque é grátis e correto; o cliente pelo gateway se orienta
            # pelo status 429.
            headers={"Retry-After": "5"},
        )
    except policy.QueueWaitTimeout as e:
        return JSONResponse(
            status_code=504,
            content={"error": {"message": str(e), "type": "timeout_error", "code": "queue_timeout"}},
        )
    except policy.WorkerStopped as e:
        # Worker interrompido com a geração em voo (tipicamente shutdown). Não
        # conta como falha de pipeline — não é sintoma de GPU quebrada.
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(e), "type": "server_error", "code": "worker_stopped"}},
        )
    except policy.ImageRequestError:
        # Erro de cliente levantado DENTRO do worker (ex.: PNG com magic byte
        # válido mas conteúdo corrompido). Não conta para a degradação.
        raise
    except Exception as e:
        # Falha real de geração: OOM de VRAM, erro de CUDA, bug no pipeline.
        _note_failure(e)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"falha na geração: {type(e).__name__}: {e}",
                    "type": "server_error",
                    "code": "generation_failed",
                }
            },
        )
    _note_success()
    return JSONResponse(
        content={
            "created": int(time.time()),
            "data": [{"b64_json": b} for b in images],
            # Parâmetros EFETIVOS da geração — não os que o cliente pediu.
            #
            # Existe por causa de quem guarda a imagem: o gateway persiste cada
            # geração no bucket (docker/gateway/image_gen.py) e precisa gravar
            # com que parâmetros ela saiu. Duas coisas ele não teria como saber
            # sozinho: a `seed`, que é sorteada aqui quando o cliente não manda
            # nenhuma; e, em /v1/images/edits, TODOS os campos — lá o corpo é
            # multipart repassado em streaming, e o gateway nunca o parseia.
            #
            # A seed é do BATCH, não de cada imagem: com n>1 o pipeline consome
            # de um único generator, e reproduzir a imagem i exige a mesma seed
            # E o mesmo n. Devolvê-la por item sugeriria uma independência que
            # não existe.
            #
            # Campo extra não quebra cliente OpenAI (SDKs ignoram desconhecidos)
            # e o conteúdo é do próprio requisitante — nada aqui é informação
            # que ele já não tenha.
            "meta": {
                "prompt": payload.prompt,
                "width": payload.width,
                "height": payload.height,
                "steps": payload.steps,
                "guidance_scale": payload.guidance_scale,
                "seed": payload.seed,
                "n": payload.n,
                "model": SERVED_MODEL_NAME,
            },
        }
    )


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    """text-to-image. Corpo JSON, no formato da OpenAI."""
    try:
        body = await request.json()
    except Exception:
        raise policy.ImageRequestError("corpo inválido (JSON esperado)", code="invalid_body")
    if not isinstance(body, dict):
        raise policy.ImageRequestError("corpo inválido (objeto JSON esperado)", code="invalid_body")

    # `image` aqui é erro de rota, não campo desconhecido: mandar referência num
    # corpo JSON é o que um cliente faria se assumisse que inventamos uma
    # extensão. Melhor apontar a rota certa que gerar text-to-image em silêncio.
    if body.get("image") is not None:
        raise policy.ImageRequestError(
            "para image-to-image use POST /v1/images/edits (multipart/form-data)",
            code="wrong_route_for_reference_image",
        )
    policy.reject_mask(body.get("mask") is not None)

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise policy.ImageRequestError("prompt é obrigatório", code="missing_prompt")

    policy.validate_model(body.get("model"), served=SERVED_MODEL_NAME, also_accept=MODEL_ALIASES)
    policy.validate_response_format(body.get("response_format"))
    width, height = policy.validate_size(
        body.get("size"), default=DEFAULT_SIZE, allowed=ALLOWED_SIZES
    )

    return await _dispatch(
        GenPayload(
            prompt=prompt,
            width=width,
            height=height,
            steps=policy.validate_steps(body.get("steps"), default=STEPS, maximum=STEPS_MAX),
            guidance_scale=policy.validate_guidance_scale(
                body.get("guidance_scale"), default=GUIDANCE_SCALE
            ),
            n=policy.validate_n(body.get("n"), maximum=IMAGES_PER_REQUEST_MAX),
            seed=policy.ensure_seed(policy.validate_seed(body.get("seed"))),
        )
    )


@app.post("/v1/images/edits")
async def images_edits(request: Request):
    """image-to-image. multipart/form-data, no formato da OpenAI.

    Lê o form manualmente em vez de declarar parâmetros `File(...)`/`Form(...)`
    porque a OpenAI usa o nome de campo `image[]` para múltiplas imagens, e
    `image[]` não é um identificador Python válido — não dá para declarar como
    parâmetro. Os dois nomes (`image` e `image[]`) alimentam a mesma lista.
    """
    # max_files no PARSER, e não só a contagem depois.
    #
    # `await request.form()` sem limite materializa o multipart INTEIRO (spool
    # em disco acima de 1 MiB) antes de qualquer checagem nossa, e o default do
    # Starlette é max_files=1000 — um corpo com 1000 arquivos era integralmente
    # processado só para depois ser recusado por "no máximo 4". A contagem
    # posterior continua (o parser aceita N+1 para podermos dar a mensagem
    # certa), mas o teto real de trabalho é imposto aqui.
    #
    # +1 e não MAX: com o limite exato, o parser aborta antes de chegarmos à
    # nossa mensagem e o cliente recebe um erro genérico em vez de
    # `too_many_reference_images`.
    try:
        form = await request.form(
            max_files=MAX_REFERENCE_IMAGES + 1,
            # campos escalares: prompt, model, size, n, steps, guidance_scale,
            # seed, response_format, mask. 32 dá folga sem permitir um corpo
            # com milhares de campos.
            max_fields=32,
        )
    except policy.ImageRequestError:
        raise
    except Exception as e:
        # MultiPartException do Starlette (excesso de arquivos/campos, corpo
        # truncado, boundary inválido) não é ImageRequestError e escaparia como
        # 500 — mas a causa é o corpo do CLIENTE, então é 400.
        raise policy.ImageRequestError(
            f"multipart inválido: {e}", code="invalid_multipart"
        ) from None

    policy.reject_mask(form.get("mask") is not None)

    uploads = [*form.getlist("image"), *form.getlist("image[]")]
    if not uploads:
        raise policy.ImageRequestError(
            "envie ao menos uma imagem de referência no campo 'image' ou 'image[]'",
            code="missing_image",
        )
    policy.collect_reference_names(uploads, maximum=MAX_REFERENCE_IMAGES)

    prompt = form.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise policy.ImageRequestError("prompt é obrigatório", code="missing_prompt")

    policy.validate_model(form.get("model"), served=SERVED_MODEL_NAME, also_accept=MODEL_ALIASES)
    policy.validate_response_format(form.get("response_format"))
    width, height = policy.validate_size(
        form.get("size"), default=DEFAULT_SIZE, allowed=ALLOWED_SIZES
    )

    references: list[bytes] = []
    for up in uploads:
        if isinstance(up, str):
            raise policy.ImageRequestError(
                "campo 'image' precisa ser um arquivo, não texto", code="invalid_image_field"
            )
        data = await up.read()
        policy.check_reference_image(
            data, max_bytes=MAX_FILE_SIZE_BYTES, allowed_formats=ALLOWED_FORMATS
        )
        references.append(data)

    return await _dispatch(
        GenPayload(
            prompt=prompt,
            width=width,
            height=height,
            steps=policy.validate_steps(form.get("steps"), default=STEPS, maximum=STEPS_MAX),
            guidance_scale=policy.validate_guidance_scale(
                form.get("guidance_scale"), default=GUIDANCE_SCALE
            ),
            n=policy.validate_n(form.get("n"), maximum=IMAGES_PER_REQUEST_MAX),
            seed=policy.ensure_seed(policy.validate_seed(form.get("seed"))),
            references=references,
        )
    )


if __name__ == "__main__":
    import uvicorn

    # host fixo em 127.0.0.1: quem expõe para o mundo é o agent na 8000, que
    # autentica. Escutar em 0.0.0.0 aqui abriria o modelo sem chave nenhuma,
    # porque o RunPod publica as portas declaradas no template.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("IMAGE_PORT", "8001")),
        log_level=os.environ.get("IMAGE_LOG_LEVEL", "info"),
    )
