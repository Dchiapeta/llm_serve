"""Política e fila do servidor de imagem: validação de request e serialização da GPU.

Funções e classes PURAS — só stdlib, sem torch, sem diffusers, sem PIL, sem
FastAPI. Importável pelos testes sem GPU e sem instalar 4 GB de wheels (mesma
disciplina de docker/agent/proxy_policy.py e docker/gateway/context_budget.py).

O server.py é a camada suja: carrega o pipeline, decodifica imagem com PIL e faz
o binding do FastAPI. Toda regra que dá para testar sem GPU vive aqui.

---------------------------------------------------------------------------
Por que a fila é de UM consumidor, e não um semáforo
---------------------------------------------------------------------------
O caminho óbvio (Semaphore(1) + asyncio.wait_for em volta do to_thread) está
errado de um jeito que só aparece sob carga: `wait_for` cancela a ESPERA, não a
thread. Num timeout ele solta o semáforo enquanto a geração anterior continua
usando a GPU, a próxima entra, e o pipeline passa a ser usado por duas threads
ao mesmo tempo — estado compartilhado corrompido, ou OOM de VRAM.

Aqui a serialização não depende de timeout nenhum: existe UMA task consumidora,
e ela roda uma geração por vez. O timeout só corta espera de FILA, que é a única
coisa que dá para abortar sem deixar a GPU num estado desconhecido.
"""

import asyncio
import random

# Formatos de entrada aceitos, detectados por magic bytes e não pela extensão
# nem pelo content-type do multipart — os dois são declarados pelo cliente. A
# detecção real também é o que impede um "png" que na verdade é um TIFF de
# 20.000×20.000 chegar ao decoder.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
)

DEFAULT_ALLOWED_FORMATS = frozenset({"png", "jpeg", "webp"})


class ImageRequestError(Exception):
    """Erro de request do cliente, com o status e o código que vão na resposta.

    `code` existe porque algumas recusas precisam ser distinguíveis por máquina,
    não só legíveis: `mask_not_supported` é o caso que motivou isso — um cliente
    tem que poder detectar "este servidor não faz inpainting" sem parsear
    português.
    """

    def __init__(self, message: str, *, status_code: int = 400, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Validação do request
# ---------------------------------------------------------------------------


def parse_size_list(raw: str) -> list[str]:
    """"1024x1024, 1536x1024" -> ["1024x1024", "1536x1024"] (env var -> allowlist)."""
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Coerção de escalares
# ---------------------------------------------------------------------------
#
# Toda a entrada chega de duas fontes ONDE O CLIENTE ESCOLHE O TIPO:
#   * corpo JSON  -> `{"steps": "abc"}`, `{"size": 1024}`, `{"seed": []}`
#   * multipart   -> `form.get("steps")` devolve UploadFile se o campo vier
#                    como ARQUIVO em vez de texto
#
# Sem estas funções, `int(raw)` e `raw.strip()` levantam ValueError/
# AttributeError, que NÃO são ImageRequestError — escapam do exception_handler
# do server e viram 500. Um corpo malformado tem que ser 400: 500 diz ao
# cliente "o servidor quebrou, tente de novo", e ele reenvia o mesmo corpo
# inválido para sempre.


def _as_text(raw, field: str) -> str | None:
    """Aceita str (ou ausência). Qualquer outro tipo é 400, não 500."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ImageRequestError(
            f"{field} deve ser texto (recebido: {type(raw).__name__})",
            code=f"invalid_{field}",
        )
    return raw


def _as_int(raw, field: str) -> int | None:
    """Inteiro tolerante a string numérica, intolerante a tudo mais.

    bool é rejeitado explicitamente porque `isinstance(True, int)` é True em
    Python: sem esta linha, `{"n": true}` passaria como n=1 e o cliente receberia
    uma imagem tendo pedido um valor sem sentido.

    Float só passa se for integral (4.0 sim, 7.9 não). `int(7.9)` trunca em
    silêncio, e aceitar isso significa gerar com 7 steps para quem pediu 7.9 —
    o cliente nunca fica sabendo.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ImageRequestError(
            f"{field} deve ser um inteiro, não booleano", code=f"invalid_{field}"
        )
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != int(raw):
            raise ImageRequestError(
                f"{field} deve ser um inteiro (recebido: {raw})", code=f"invalid_{field}"
            )
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            raise ImageRequestError(
                f"{field} deve ser um inteiro (recebido: {raw!r})",
                code=f"invalid_{field}",
            ) from None
    raise ImageRequestError(
        f"{field} deve ser um inteiro (recebido: {type(raw).__name__})",
        code=f"invalid_{field}",
    )


def _as_float(raw, field: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ImageRequestError(
            f"{field} deve ser um número, não booleano", code=f"invalid_{field}"
        )
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            raise ImageRequestError(
                f"{field} deve ser um número (recebido: {raw!r})",
                code=f"invalid_{field}",
            ) from None
    raise ImageRequestError(
        f"{field} deve ser um número (recebido: {type(raw).__name__})",
        code=f"invalid_{field}",
    )


def validate_size(raw, *, default: str, allowed: list[str]) -> tuple[int, int]:
    """Resolve `size` para (width, height) contra uma allowlist fechada.

    Allowlist, e não um range com passo de 64: em difusão a resolução decide o
    custo de VRAM e de tempo do passo, então resolução arbitrária é DoS barato —
    um cliente pedindo 4096×4096 ocuparia o único worker por minutos. As três
    resoluções permitidas foram dimensionadas para a A40.
    """
    value = (_as_text(raw, "size") or default).strip()
    if value not in allowed:
        raise ImageRequestError(
            f"size inválido: {value!r}. Aceitos: {', '.join(allowed)}",
            code="invalid_size",
        )
    width, height = value.split("x")
    return int(width), int(height)


def validate_model(raw, *, served: str, also_accept: frozenset[str] = frozenset()) -> str:
    """Aceita `model` ausente ou igual ao alias servido; qualquer outro é 400.

    Mesma disciplina do pin_model do gateway: o servidor decide o modelo. O pod
    carrega UM pipeline; aceitar um `model` diferente em silêncio faria o cliente
    acreditar que pediu outra coisa e recebeu o que pediu.
    """
    raw = _as_text(raw, "model")
    if raw is None or raw == "":
        return served
    if raw != served and raw not in also_accept:
        raise ImageRequestError(
            f"model {raw!r} não é servido por este pod (servido: {served!r})",
            code="model_not_found",
            status_code=404,
        )
    return served


def validate_response_format(raw) -> str:
    """Só b64_json. `url` exigiria storage, que não existe nesta versão."""
    value = _as_text(raw, "response_format") or "b64_json"
    if value == "url":
        raise ImageRequestError(
            "response_format 'url' não é suportado: este pod não persiste imagem. "
            "Use 'b64_json'.",
            code="response_format_not_supported",
        )
    if value != "b64_json":
        raise ImageRequestError(
            f"response_format inválido: {value!r}. Aceito: 'b64_json'",
            code="invalid_response_format",
        )
    return value


def reject_mask(mask_present: bool) -> None:
    """Máscara nunca é ignorada em silêncio.

    O Flux2KleinInpaintPipeline EXISTE no diffusers 0.40 e aceita `mask_image` —
    o motivo do 400 não é o modelo, é que este pod carrega só o
    Flux2KleinPipeline e uma segunda classe de pipeline está fora desta versão.
    O perigo de aceitar e ignorar é pior que o de recusar: o cliente receberia
    uma imagem editada inteira achando que só a região da máscara mudou.
    """
    if mask_present:
        raise ImageRequestError(
            "edição por máscara não é suportada nesta versão",
            code="mask_not_supported",
        )


def validate_n(raw, *, maximum: int) -> int:
    parsed = _as_int(raw, "n")
    value = 1 if parsed is None else parsed
    if value < 1 or value > maximum:
        raise ImageRequestError(
            f"n deve estar entre 1 e {maximum} (pedido: {value})", code="invalid_n"
        )
    return value


def validate_steps(raw, *, default: int, maximum: int) -> int:
    """Teto em steps pelo mesmo motivo da allowlist de size: é tempo de GPU.

    O modelo é distilled (`is_distilled: true` no model_index.json), então 4
    steps é o ponto de operação; o teto existe só para que um cliente não peça
    100 e monopolize o worker.
    """
    parsed = _as_int(raw, "steps")
    value = default if parsed is None else parsed
    if value < 1 or value > maximum:
        raise ImageRequestError(
            f"steps deve estar entre 1 e {maximum} (pedido: {value})", code="invalid_steps"
        )
    return value


def validate_guidance_scale(raw, *, default: float) -> float:
    parsed = _as_float(raw, "guidance_scale")
    value = default if parsed is None else parsed
    if value < 0.0 or value > 20.0:
        raise ImageRequestError(
            f"guidance_scale deve estar entre 0 e 20 (pedido: {value})",
            code="invalid_guidance_scale",
        )
    return value


SEED_MAX = 2**64 - 1


def validate_seed(raw) -> int | None:
    value = _as_int(raw, "seed")
    if value is None:
        return None
    # 2**64-1: teto do manual_seed do torch. Acima disso ele levanta, e o erro
    # sairia como 500 em vez de 400.
    if value < 0 or value > SEED_MAX:
        raise ImageRequestError("seed fora da faixa [0, 2^64-1]", code="invalid_seed")
    return value


def ensure_seed(value: int | None) -> int:
    """Seed efetiva da geração: a do cliente, ou uma sorteada aqui.

    Sortear NESTE nível, em vez de deixar o torch decidir sozinho quando o
    generator é None, é o que torna a geração descritível: a resposta passa a
    poder dizer com que seed a imagem saiu, e quem guardar esse valor consegue
    reproduzi-la. Enquanto o sorteio ficava implícito lá dentro, toda requisição
    sem `seed` produzia uma imagem que ninguém — nem nós — sabia repetir.

    random e não secrets: seed de imagem é um identificador de resultado, não um
    segredo. Previsibilidade aqui não abre risco nenhum.
    """
    if value is not None:
        return value
    return random.getrandbits(64)


def detect_format(data: bytes) -> str | None:
    """Formato por magic bytes. None = não reconhecido.

    WebP fica fora da tabela _MAGIC porque o cabeçalho é partido: "RIFF" nos
    bytes 0-3 e "WEBP" nos 8-11, com o tamanho no meio. Checar só "RIFF" daria
    match em WAV e AVI.
    """
    for magic, name in _MAGIC:
        if data.startswith(magic):
            return name
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def check_reference_image(
    data: bytes, *, max_bytes: int, allowed_formats: frozenset[str]
) -> str:
    """Valida uma imagem de referência e devolve o formato detectado."""
    if len(data) > max_bytes:
        raise ImageRequestError(
            f"imagem de referência excede {max_bytes // (1024 * 1024)} MB",
            status_code=413,
            code="image_too_large",
        )
    if not data:
        raise ImageRequestError("imagem de referência vazia", code="empty_image")
    fmt = detect_format(data)
    if fmt is None or fmt not in allowed_formats:
        raise ImageRequestError(
            f"formato de imagem não suportado (detectado: {fmt or 'desconhecido'}). "
            f"Aceitos: {', '.join(sorted(allowed_formats))}",
            code="unsupported_image_format",
        )
    return fmt


def collect_reference_names(field_names: list[str], *, maximum: int) -> None:
    """Valida a QUANTIDADE de referências e produz a mensagem de erro certa.

    Recebe só os nomes (ou qualquer lista do mesmo tamanho: só usa len()).

    ATENÇÃO ao que esta função NÃO faz: ela roda DEPOIS de `request.form()`, que
    já materializou o multipart inteiro. Quem limita o trabalho de parsing é o
    `max_files` passado ao form no server.py — sem ele, um corpo com 1000
    arquivos era processado por completo só para ser recusado aqui. Esta
    checagem existe para dar o erro `too_many_reference_images` em vez do erro
    genérico do parser.
    """
    if len(field_names) > maximum:
        raise ImageRequestError(
            f"no máximo {maximum} imagens de referência (enviadas: {len(field_names)})",
            code="too_many_reference_images",
        )


# ---------------------------------------------------------------------------
# Fila de geração
# ---------------------------------------------------------------------------


class QueueFull(Exception):
    """Fila cheia: 429. O cliente pode tentar de novo, nada foi perdido."""


class QueueWaitTimeout(Exception):
    """Esperou demais NA FILA e desistiu: 504. A GPU nunca foi tocada."""


class WorkerStopped(Exception):
    """A task consumidora foi interrompida com uma geração em voo.

    Existe para que quem está esperando o resultado ACORDE. Sem ela, o
    `asyncio.shield` do submit — que é o que protege o Future de um
    cancelamento do handler — garante que o awaiter fique pendurado para
    sempre quando é o WORKER que morre, e não o handler. No shutdown do
    uvicorn isso é uma request que nunca termina.
    """


class Job:
    __slots__ = ("payload", "started", "future", "cancelled")

    def __init__(self, payload, loop: asyncio.AbstractEventLoop):
        self.payload = payload
        # started é o handshake que separa "ainda dá para desistir" de "já está
        # na GPU". É o que torna o timeout seguro.
        self.started = asyncio.Event()
        self.future: asyncio.Future = loop.create_future()
        self.cancelled = False


def _swallow_orphan_exception(fut: asyncio.Future) -> None:
    """Marca a exceção como lida, para o caso de ninguém mais estar esperando.

    Quando o handler é cancelado (cliente desconectou), o `shield` mantém o
    Future vivo mas ninguém consome o resultado. Se for exceção, o asyncio
    imprime "Future exception was never retrieved" no log do pod a cada
    desconexão. Chamar .exception() aqui só marca como recuperada — quem estiver
    de fato aguardando continua recebendo o raise normalmente.
    """
    if not fut.cancelled():
        fut.exception()


class GenerationQueue:
    """Fila com UM consumidor: a garantia de que duas gerações nunca se sobrepõem.

    `run` é um callable SÍNCRONO (a chamada do pipeline). Ele é executado em
    asyncio.to_thread pelo consumidor — nunca inline no event loop, senão o
    /health para de responder enquanto a GPU trabalha e o reconciliador do
    painel marca a máquina como "Falha" (docker/agent/main.py:health).
    """

    def __init__(self, run, *, capacity: int, wait_timeout_s: float):
        self._run = run
        # Sem maxsize: o teto é o contador `_in_flight` abaixo, checado
        # SINCRONAMENTE no submit.
        #
        # Com maxsize, a fronteira do 429 depende de escalonamento: num burst
        # que chega todo no mesmo tick do event loop, nenhum put_nowait teve o
        # worker rodando entre eles, então o teto observado é `capacity`; se o
        # worker roda no meio (uma requisição já em curso), passa a ser
        # `capacity + 1`. O cliente veria o 429 num ponto diferente conforme a
        # sorte do agendamento — inaceitável num contrato de API.
        #
        # `capacity` aqui é TOTAL EM VOO (em execução + esperando), que também
        # é o número que interessa ao cliente: quantas requisições minhas o pod
        # aceita antes de me mandar tentar depois.
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._capacity = capacity
        self._in_flight = 0
        self._wait_timeout_s = wait_timeout_s
        self._worker: asyncio.Task | None = None
        # observabilidade: quantas gerações rodaram e quantas se sobrepuseram.
        # `overlaps` é sempre 0 por construção; se algum dia não for, é bug de
        # invariante e a métrica é o que denuncia.
        self.completed = 0
        self.overlaps = 0
        self._running = 0
        # Preenchido por _on_worker_done. None = worker nunca morreu por erro.
        self.worker_error: str | None = None

    def start(self) -> None:
        # `or self._worker.done()`: sem isso, uma task consumidora que morreu
        # nunca é substituída — start() virava no-op e TODO submit seguinte
        # esperava o timeout inteiro e devolvia 504, indefinidamente, com o
        # /health continuando 200.
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._consume(), name="image-worker")
            self._worker.add_done_callback(self._on_worker_done)

    def _on_worker_done(self, task: asyncio.Task) -> None:
        """Registra a morte da task consumidora para o /health poder denunciá-la.

        Um pod cujo worker morreu responde /health 200 e aceita requests que
        todas expiram em 504 — para o reconciliador ele está "running" e para o
        reaper está em uso. Guardar o erro aqui é o que permite ao /health
        devolver 503 e a máquina ser tratada como falha.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.worker_error = f"{type(exc).__name__}: {exc}"

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    @property
    def depth(self) -> int:
        """Jobs esperando para serem pegos pelo worker."""
        return self._queue.qsize()

    @property
    def in_flight(self) -> int:
        """Aceitos e ainda não respondidos (em execução + esperando)."""
        return self._in_flight

    @property
    def alive(self) -> bool:
        """A task consumidora existe e está rodando.

        Lido pelo /health: sem worker não há geração possível, e o pod tem que
        se declarar não-pronto em vez de aceitar requests que só expiram.
        """
        return self._worker is not None and not self._worker.done()

    async def submit(self, payload):
        # Checagem síncrona, antes de qualquer await: é o que torna a fronteira
        # do 429 determinística.
        if self._in_flight >= self._capacity:
            raise QueueFull(f"fila de geração cheia ({self._capacity} em voo)")

        loop = asyncio.get_running_loop()
        job = Job(payload, loop)
        job.future.add_done_callback(_swallow_orphan_exception)

        self._in_flight += 1
        try:
            self._queue.put_nowait(job)

            try:
                await asyncio.wait_for(job.started.wait(), self._wait_timeout_s)
            except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                # CancelledError entra JUNTO com TimeoutError, e não é detalhe:
                # `wait_for` levanta CancelledError — não TimeoutError — quando
                # é a task do handler que é cancelada. Tratando só o timeout, o
                # `finally` abaixo liberava a vaga de _in_flight enquanto o job
                # continuava na fila SEM `cancelled=True`; o worker então gerava
                # a imagem para um cliente que não existe mais, a fila crescia
                # sem teto e o contador de admissão mentia. Medido: capacity=2
                # admitindo 21 gerações.
                #
                # Re-check obrigatório: o worker pode ter dado started.set() no
                # mesmo tick. Sem isto, uma geração JÁ EM CURSO seria marcada
                # como cancelada e o job seguinte veria `cancelled=True` num job
                # que o worker já passou.
                if not job.started.is_set():
                    job.cancelled = True
                    # Solta as referências (até 4 × 15 MB) agora, em vez de
                    # deixá-las presas no job até o worker chegar nele — o
                    # worker pode estar minutos dentro de uma geração.
                    job.payload = None
                    if not job.future.done():
                        job.future.cancel()
                    if isinstance(e, asyncio.CancelledError):
                        raise
                    raise QueueWaitTimeout(
                        f"espera na fila excedeu {self._wait_timeout_s:.0f}s"
                    ) from None
                # Já começou: cancelamento do handler não aborta a GPU. Propaga
                # o cancelamento, mas o worker segue e resolve o Future.
                if isinstance(e, asyncio.CancelledError):
                    raise

            # Daqui para frente a geração está na GPU e não é abortável. O
            # shield impede que um cancelamento do handler (cliente
            # desconectou) cancele o Future que o worker vai preencher — o que
            # faria o set_result dele levantar InvalidStateError e derrubar a
            # task consumidora, matando o pod para todos os clientes seguintes.
            return await asyncio.shield(job.future)
        finally:
            # Libera a vaga inclusive no caminho de timeout e no de
            # cancelamento. O job cancelado continua na fila até o worker
            # pegá-lo e descartá-lo — custo O(1), sem geração nenhuma.
            self._in_flight -= 1

    async def _consume(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.cancelled:
                    # Desistiu enquanto esperava: a GPU nunca foi tocada.
                    continue
                job.started.set()
                self._running += 1
                if self._running > 1:  # pragma: no cover - invariante
                    self.overlaps += 1
                try:
                    out = await asyncio.to_thread(self._run, job.payload)
                except asyncio.CancelledError:
                    # Acordar quem espera é obrigatório: o `asyncio.shield` do
                    # submit protege o Future de um cancelamento do HANDLER, e
                    # por isso mesmo impede que o awaiter perceba a morte do
                    # WORKER. Sem este set_exception, um stop() no meio de uma
                    # geração (shutdown do uvicorn) deixa a request pendurada
                    # para sempre.
                    if not job.future.done():
                        job.future.set_exception(
                            WorkerStopped("worker interrompido durante a geração")
                        )
                    raise
                except Exception as e:
                    if not job.future.done():
                        job.future.set_exception(e)
                else:
                    self.completed += 1
                    if not job.future.done():
                        job.future.set_result(out)
                finally:
                    self._running -= 1
            finally:
                self._queue.task_done()
