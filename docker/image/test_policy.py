"""Testes da política e da fila do servidor de imagem.

Puros: importam só `policy` (stdlib), então rodam sem GPU, sem torch e sem
diffusers. Async no estilo do resto do repo — `asyncio.run` dentro de teste
síncrono, porque não há pytest-asyncio instalado
(ver docker/gateway/test_stream_watchdog.py).

    python3 -m pytest docker/image/test_policy.py -q
"""

import asyncio
import threading

import pytest

import policy

ALLOWED_SIZES = ["1024x1024", "1536x1024", "1024x1536"]


# ---------------------------------------------------------------------------
# size
# ---------------------------------------------------------------------------


def test_size_ausente_usa_o_default():
    assert policy.validate_size(None, default="1024x1024", allowed=ALLOWED_SIZES) == (
        1024,
        1024,
    )


def test_size_nao_quadrado_devolve_width_height_na_ordem():
    """1536x1024 é WIDTHxHEIGHT. Trocar a ordem geraria retrato onde o cliente
    pediu paisagem, sem erro nenhum — só a imagem errada."""
    assert policy.validate_size("1536x1024", default="1024x1024", allowed=ALLOWED_SIZES) == (
        1536,
        1024,
    )


def test_size_fora_da_allowlist_e_recusado():
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_size("4096x4096", default="1024x1024", allowed=ALLOWED_SIZES)
    assert e.value.status_code == 400
    assert e.value.code == "invalid_size"


def test_parse_size_list_tolera_espaco():
    assert policy.parse_size_list("1024x1024, 1536x1024 ,1024x1536") == ALLOWED_SIZES


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


def test_model_ausente_assume_o_servido():
    assert policy.validate_model(None, served="flux2-klein-4b") == "flux2-klein-4b"
    assert policy.validate_model("", served="flux2-klein-4b") == "flux2-klein-4b"


def test_model_divergente_e_404_e_nao_200_silencioso():
    """O pod carrega UM pipeline. Aceitar outro nome faria o cliente acreditar
    que pediu dall-e e recebeu dall-e."""
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_model("dall-e-3", served="flux2-klein-4b")
    assert e.value.status_code == 404
    assert e.value.code == "model_not_found"


# ---------------------------------------------------------------------------
# response_format / mask
# ---------------------------------------------------------------------------


def test_response_format_default_e_b64():
    assert policy.validate_response_format(None) == "b64_json"


def test_response_format_url_e_recusado_com_codigo_proprio():
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_response_format("url")
    assert e.value.code == "response_format_not_supported"


def test_mask_e_recusada_nunca_ignorada():
    """Ignorar a máscara devolveria a imagem editada INTEIRA com status 200 —
    o cliente não teria como saber que a região que ele delimitou não foi
    respeitada."""
    policy.reject_mask(False)  # não levanta
    with pytest.raises(policy.ImageRequestError) as e:
        policy.reject_mask(True)
    assert e.value.code == "mask_not_supported"
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# n / steps / guidance / seed
# ---------------------------------------------------------------------------


def test_n_respeita_o_teto():
    assert policy.validate_n(None, maximum=1) == 1
    assert policy.validate_n("1", maximum=1) == 1
    with pytest.raises(policy.ImageRequestError):
        policy.validate_n(2, maximum=1)
    with pytest.raises(policy.ImageRequestError):
        policy.validate_n(0, maximum=1)


def test_steps_tem_teto_porque_e_tempo_de_gpu():
    assert policy.validate_steps(None, default=4, maximum=8) == 4
    assert policy.validate_steps("8", default=4, maximum=8) == 8
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_steps(100, default=4, maximum=8)
    assert e.value.code == "invalid_steps"


def test_guidance_scale_aceita_o_1_0_do_modelo_distilled():
    assert policy.validate_guidance_scale(None, default=1.0) == 1.0
    assert policy.validate_guidance_scale("1.0", default=1.0) == 1.0
    with pytest.raises(policy.ImageRequestError):
        policy.validate_guidance_scale(50, default=1.0)


def test_seed_acima_do_teto_do_torch_e_400_e_nao_500():
    assert policy.validate_seed(None) is None
    assert policy.validate_seed("42") == 42
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_seed(2**64)
    assert e.value.status_code == 400


# ---------------------------------------------------------------------------
# imagem de referência
# ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 32
WAV = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"\x00" * 32


def test_detecta_os_tres_formatos_aceitos():
    assert policy.detect_format(PNG) == "png"
    assert policy.detect_format(JPEG) == "jpeg"
    assert policy.detect_format(WEBP) == "webp"


def test_riff_que_nao_e_webp_nao_passa():
    """Checar só "RIFF" daria match em WAV e AVI — o cabeçalho do WebP é
    partido, com o tamanho entre "RIFF" e "WEBP"."""
    assert policy.detect_format(WAV) is None


def test_formato_vem_dos_magic_bytes_nao_da_extensao():
    """O content-type e o nome do arquivo são declarados pelo cliente. Um .png
    que é outra coisa tem que ser recusado antes do decoder."""
    with pytest.raises(policy.ImageRequestError) as e:
        policy.check_reference_image(
            b"GIF89a" + b"\x00" * 32,
            max_bytes=15 * 1024 * 1024,
            allowed_formats=policy.DEFAULT_ALLOWED_FORMATS,
        )
    assert e.value.code == "unsupported_image_format"


def test_imagem_grande_e_413():
    with pytest.raises(policy.ImageRequestError) as e:
        policy.check_reference_image(
            PNG + b"\x00" * (15 * 1024 * 1024),
            max_bytes=15 * 1024 * 1024,
            allowed_formats=policy.DEFAULT_ALLOWED_FORMATS,
        )
    assert e.value.status_code == 413
    assert e.value.code == "image_too_large"


def test_imagem_vazia_e_recusada():
    with pytest.raises(policy.ImageRequestError) as e:
        policy.check_reference_image(
            b"", max_bytes=15 * 1024 * 1024, allowed_formats=policy.DEFAULT_ALLOWED_FORMATS
        )
    assert e.value.code == "empty_image"


def test_teto_de_referencias_produz_o_codigo_de_erro_certo():
    # O teto de TRABALHO é imposto pelo max_files do request.form() no
    # server.py; esta função existe para a mensagem/código serem os nossos em
    # vez do erro genérico do parser do Starlette.
    policy.collect_reference_names(["a", "b", "c", "d"], maximum=4)
    with pytest.raises(policy.ImageRequestError) as e:
        policy.collect_reference_names(["a", "b", "c", "d", "e"], maximum=4)
    assert e.value.code == "too_many_reference_images"


# ---------------------------------------------------------------------------
# GenerationQueue
# ---------------------------------------------------------------------------


class _Recorder:
    """Worker falso que registra entrada/saída de cada geração.

    Bloqueia num threading.Event porque o `run` da fila é síncrono e roda em
    to_thread — asyncio.Event não serve, o loop não está naquela thread.
    """

    def __init__(self):
        self.intervals: list[tuple[str, int]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, payload):
        with self.lock:
            self.intervals.append(("in", payload))
        self.entered.set()
        self.release.wait(timeout=5)
        with self.lock:
            self.intervals.append(("out", payload))
        return f"img-{payload}"


def _fast(payload):
    return f"img-{payload}"


def test_fila_cheia_devolve_queue_full_e_nao_enfileira_infinito():
    """`capacity` é TOTAL EM VOO: em execução + esperando.

    Com maxsize no asyncio.Queue esse número oscilava entre `capacity` e
    `capacity + 1` conforme o worker tivesse sido escalonado entre os
    put_nowait — o cliente veria o 429 num ponto diferente a cada burst."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=4, wait_timeout_s=5)
        q.start()
        tasks = [asyncio.create_task(q.submit(i)) for i in range(4)]
        for _ in range(200):
            if rec.entered.is_set() and q.in_flight == 4:
                break
            await asyncio.sleep(0.01)
        assert q.in_flight == 4

        with pytest.raises(policy.QueueFull):
            await q.submit(99)

        rec.release.set()
        await asyncio.gather(*tasks)
        assert q.in_flight == 0
        await q.stop()

    asyncio.run(go())


def test_admissao_e_deterministica_num_burst_do_mesmo_tick():
    """8 simultâneos com capacity=4 => exatamente 4 aceitos e 4 recusados,
    independente de agendamento. É esse número que a documentação promete."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=4, wait_timeout_s=5)
        q.start()

        async def tentar(i):
            try:
                return await q.submit(i)
            except policy.QueueFull:
                return "429"

        tasks = [asyncio.create_task(tentar(i)) for i in range(8)]
        for _ in range(200):
            if rec.entered.is_set():
                break
            await asyncio.sleep(0.01)
        rec.release.set()
        resultados = await asyncio.gather(*tasks)

        assert resultados.count("429") == 4
        assert len([r for r in resultados if r != "429"]) == 4
        await q.stop()

    asyncio.run(go())


def test_geracoes_nunca_se_sobrepoem():
    """O invariante que define o desenho. Um Semaphore(1) + wait_for passaria
    nos outros testes e falharia neste sob timeout."""

    async def go():
        intervals = []
        lock = threading.Lock()

        def run(payload):
            with lock:
                intervals.append(("in", payload))
            # janela real de execução, para que uma sobreposição apareça
            threading.Event().wait(0.05)
            with lock:
                intervals.append(("out", payload))
            return payload

        q = policy.GenerationQueue(run, capacity=8, wait_timeout_s=5)
        q.start()
        await asyncio.gather(*[q.submit(i) for i in range(4)])
        await q.stop()

        # sequência tem que ser in,out,in,out… nunca in,in
        depth = 0
        for kind, _ in intervals:
            depth += 1 if kind == "in" else -1
            assert depth <= 1, f"duas gerações simultâneas: {intervals}"
        assert q.completed == 4
        assert q.overlaps == 0

    asyncio.run(go())


def test_timeout_na_fila_nao_mata_a_geracao_em_curso():
    """O bug que o desenho evita: o 504 do segundo cliente não pode abortar
    nem contaminar a geração do primeiro."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=4, wait_timeout_s=0.1)
        q.start()

        primeiro = asyncio.create_task(q.submit("a"))
        for _ in range(100):
            if rec.entered.is_set():
                break
            await asyncio.sleep(0.01)

        # segundo entra na fila e desiste antes de começar
        with pytest.raises(policy.QueueWaitTimeout):
            await q.submit("b")

        rec.release.set()
        assert await primeiro == "img-a"
        await q.stop()

        # "b" nunca chegou à GPU
        assert [p for k, p in rec.intervals if k == "in"] == ["a"]
        assert q.completed == 1

    asyncio.run(go())


def test_handler_cancelado_nao_derruba_o_worker():
    """Cliente desconecta no meio. Sem o shield + a guarda de future.done(), o
    set_result do worker levantava InvalidStateError e matava a task
    consumidora — o pod parava de gerar para TODOS os clientes seguintes."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=4, wait_timeout_s=5)
        q.start()

        abandonado = asyncio.create_task(q.submit("a"))
        for _ in range(100):
            if rec.entered.is_set():
                break
            await asyncio.sleep(0.01)
        abandonado.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandonado

        rec.release.set()
        await asyncio.sleep(0.05)

        # o worker sobreviveu: um job novo ainda é atendido
        rec.entered.clear()
        rec.release.set()
        assert await q.submit("b") == "img-b"
        assert q.completed == 2
        await q.stop()

    asyncio.run(go())


def test_excecao_da_geracao_chega_ao_cliente():
    async def go():
        def explode(_payload):
            raise RuntimeError("CUDA out of memory")

        q = policy.GenerationQueue(explode, capacity=4, wait_timeout_s=5)
        q.start()
        with pytest.raises(RuntimeError, match="CUDA out of memory"):
            await q.submit("a")
        # e o worker continua vivo depois de uma falha
        q_ok = policy.GenerationQueue(_fast, capacity=4, wait_timeout_s=5)
        q_ok.start()
        assert await q_ok.submit("b") == "img-b"
        await q.stop()
        await q_ok.stop()

    asyncio.run(go())


def test_worker_sobrevive_a_excecao_e_atende_o_proximo():
    """Falha de uma geração não pode ser falha do pod."""

    async def go():
        chamadas = []

        def as_vezes_explode(payload):
            chamadas.append(payload)
            if payload == "a":
                raise RuntimeError("boom")
            return f"img-{payload}"

        q = policy.GenerationQueue(as_vezes_explode, capacity=4, wait_timeout_s=5)
        q.start()
        with pytest.raises(RuntimeError):
            await q.submit("a")
        assert await q.submit("b") == "img-b"
        assert chamadas == ["a", "b"]
        await q.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Coerção de tipos: corpo malformado é 400, nunca 500
# ---------------------------------------------------------------------------
#
# Regressão medida antes da correção: 13 combinações devolviam 500. Um 500 diz
# ao cliente "o servidor quebrou, tente de novo", e ele reenvia o mesmo corpo
# inválido para sempre.


@pytest.mark.parametrize(
    "raw",
    [1024, ["1024x1024"], {"w": 1024}, 3.5, True],
)
def test_size_de_tipo_errado_e_400_nao_500(raw):
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_size(raw, default="1024x1024", allowed=ALLOWED_SIZES)
    assert e.value.status_code == 400


@pytest.mark.parametrize("raw", ["abc", {}, [], 7.9, "4.5"])
def test_steps_de_tipo_errado_e_400_nao_500(raw):
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_steps(raw, default=4, maximum=8)
    assert e.value.status_code == 400


@pytest.mark.parametrize("raw", ["abc", [], {}, 1.5])
def test_n_de_tipo_errado_e_400_nao_500(raw):
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_n(raw, maximum=1)
    assert e.value.status_code == 400


@pytest.mark.parametrize("raw", ["alto", [], {}])
def test_guidance_de_tipo_errado_e_400_nao_500(raw):
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_guidance_scale(raw, default=1.0)
    assert e.value.status_code == 400


@pytest.mark.parametrize("raw", ["abc", [], {}, 1.9])
def test_seed_de_tipo_errado_e_400_nao_500(raw):
    with pytest.raises(policy.ImageRequestError) as e:
        policy.validate_seed(raw)
    assert e.value.status_code == 400


@pytest.mark.parametrize("raw", [1024, ["x"], {}, 1.0])
def test_model_e_response_format_de_tipo_errado_sao_400(raw):
    with pytest.raises(policy.ImageRequestError):
        policy.validate_model(raw, served="flux2-klein-4b")
    with pytest.raises(policy.ImageRequestError):
        policy.validate_response_format(raw)


def test_booleano_nao_passa_por_inteiro():
    """isinstance(True, int) é True em Python: sem a guarda explícita,
    {"n": true} viraria n=1 e o cliente receberia imagem tendo pedido lixo."""
    with pytest.raises(policy.ImageRequestError):
        policy.validate_n(True, maximum=1)
    with pytest.raises(policy.ImageRequestError):
        policy.validate_steps(True, default=4, maximum=8)


def test_float_integral_passa_float_truncante_nao():
    """int(7.9) trunca em silêncio: aceitar isso é gerar com 7 steps para quem
    pediu 7.9, sem o cliente saber."""
    assert policy.validate_steps(4.0, default=4, maximum=8) == 4
    with pytest.raises(policy.ImageRequestError):
        policy.validate_steps(7.9, default=4, maximum=8)


def test_string_numerica_continua_aceita():
    """Multipart entrega tudo como string — rejeitá-las quebraria a rota."""
    assert policy.validate_steps("4", default=4, maximum=8) == 4
    assert policy.validate_n("1", maximum=1) == 1
    assert policy.validate_guidance_scale("1.5", default=1.0) == 1.5
    assert policy.validate_seed("42") == 42


def test_model_aceita_nome_adicional():
    """O gateway sobrescreve `model` com machines.model_name (o path do HF),
    porque este template não tem --served-model-name para o
    vllmFlagsFromTemplate extrair. Sem also_accept seria 404 em 100% das
    requests quando images/* entrar no ALLOWED_V1 do gateway."""
    hf = "black-forest-labs/FLUX.2-klein-4B"
    assert policy.validate_model(hf, served="flux2-klein-4b", also_accept=frozenset({hf})) == "flux2-klein-4b"
    with pytest.raises(policy.ImageRequestError):
        policy.validate_model("outro/modelo", served="flux2-klein-4b", also_accept=frozenset({hf}))


# ---------------------------------------------------------------------------
# Fila: cancelamento do handler, morte do worker
# ---------------------------------------------------------------------------


def test_handler_cancelado_ANTES_do_start_nao_fura_a_admissao():
    """O bug: asyncio.wait_for levanta CancelledError, não TimeoutError, quando
    a task do handler é cancelada. Tratando só TimeoutError, o finally liberava
    a vaga de _in_flight e o job ficava na fila SEM cancelled=True — o worker
    gerava a imagem para um cliente que não existe mais.

    Medido antes da correção: capacity=2 admitindo 21 gerações."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=2, wait_timeout_s=30)
        q.start()

        # um job ocupa o worker
        ocupado = asyncio.create_task(q.submit("ocupa"))
        for _ in range(200):
            if rec.entered.is_set():
                break
            await asyncio.sleep(0.01)

        # 20 handlers entram na fila e são abandonados
        for i in range(20):
            t = asyncio.create_task(q.submit(f"abandonado-{i}"))
            await asyncio.sleep(0)
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t

        rec.release.set()
        await ocupado
        # deixa o worker drenar os cancelados
        for _ in range(200):
            if q.depth == 0:
                break
            await asyncio.sleep(0.01)

        gerados = [p for k, p in rec.intervals if k == "in"]
        assert gerados == ["ocupa"], f"job abandonado foi para a GPU: {gerados}"
        assert q.completed == 1
        assert q.in_flight == 0
        await q.stop()

    asyncio.run(go())


def test_job_abandonado_solta_o_payload():
    """As referências podem ser 4 × 15 MB. Deixá-las presas no job até o worker
    chegar nele significa segurar 60 MB enquanto ele está minutos dentro de uma
    geração."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=4, wait_timeout_s=0.05)
        q.start()
        ocupado = asyncio.create_task(q.submit("ocupa"))
        for _ in range(200):
            if rec.entered.is_set():
                break
            await asyncio.sleep(0.01)

        with pytest.raises(policy.QueueWaitTimeout):
            await q.submit(["payload", "grande"])

        # o job cancelado ainda está na fila, mas sem o payload
        assert q.depth == 1
        job = q._queue._queue[0]  # inspeção interna proposital
        assert job.cancelled is True
        assert job.payload is None

        rec.release.set()
        await ocupado
        await q.stop()

    asyncio.run(go())


def test_worker_cancelado_em_voo_acorda_quem_espera():
    """O shield protege o Future de um cancelamento do HANDLER — e por isso
    mesmo impede que o awaiter perceba a morte do WORKER. Sem o set_exception
    no except CancelledError do _consume, um stop() no meio de uma geração
    (shutdown do uvicorn) deixa a request pendurada para sempre."""

    async def go():
        rec = _Recorder()
        q = policy.GenerationQueue(rec, capacity=4, wait_timeout_s=30)
        q.start()
        pendente = asyncio.create_task(q.submit("a"))
        for _ in range(200):
            if rec.entered.is_set():
                break
            await asyncio.sleep(0.01)

        await q.stop()  # cancela o worker com a geração em voo
        rec.release.set()

        with pytest.raises(policy.WorkerStopped):
            await asyncio.wait_for(pendente, timeout=3)

    asyncio.run(go())


def test_worker_morto_e_observavel_e_reiniciavel():
    """Worker morto respondia 504 em toda request para sempre, com /health
    continuando 200 — máquina saudável no painel, erro em tudo."""

    async def go():
        q = policy.GenerationQueue(_fast, capacity=4, wait_timeout_s=0.2)
        q.start()
        assert q.alive is True
        assert await q.submit("a") == "img-a"

        q._worker.cancel()
        try:
            await q._worker
        except asyncio.CancelledError:
            pass
        assert q.alive is False

        # antes da correção, start() era no-op quando _worker != None
        q.start()
        assert q.alive is True
        assert await q.submit("b") == "img-b"
        await q.stop()

    asyncio.run(go())


def test_erro_do_worker_fica_registrado():
    """O /health precisa de um motivo para reportar, não só de um booleano."""

    async def go():
        async def suicida():
            raise RuntimeError("morri")

        q = policy.GenerationQueue(_fast, capacity=4, wait_timeout_s=1)
        q._worker = asyncio.create_task(suicida())
        q._worker.add_done_callback(q._on_worker_done)
        with pytest.raises(RuntimeError):
            await q._worker
        assert q.alive is False
        assert q.worker_error is not None and "morri" in q.worker_error

    asyncio.run(go())
