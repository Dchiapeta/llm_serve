"""Filtro de raciocínio do chat/completions, e a costura de streaming dele.

Módulo separado pelo mesmo motivo do stream_watchdog.py: main.py não é
importável sem fastapi e o resto das deps de runtime, então nada que viva lá
é testável isoladamente. Este filtro nasceu dentro do main.py e foi por isso
que o bug abaixo passou meses sem ser pego — a suíte cobria a versão do
/v1/messages (que importa limpo) e não esta. A COSTURA
(`filtered_reasoning_stream`) veio junto pelo mesmo motivo: sem ela aqui, os
caminhos de timeout/queda continuariam sem teste.

O BUG que motivou tudo: o vLLM renomeou o campo de raciocínio de
"reasoning_content" para "reasoning" (confirmado na doc da 0.24.0, versão que
roda em produção). A guarda que DESLIGA este filtro testava só o nome antigo,
então nunca disparava; e como o parser do vLLM remove as tags, o </think> que
fecharia o buffer também nunca chegava no content. Resultado: a resposta
inteira represada até o fim do stream, e — quando o modelo gastava o teto de
tokens pensando — resposta VAZIA gravada como sucesso com milhares de tokens
cobrados (medido: 24.000 de 72.666 tokens de um load test).

Aceita os DOIS nomes de propósito: imagem antiga ainda manda o velho, e trocar
um pelo outro só moveria o mesmo bug de lugar.

INVARIANTE que o resto do módulo protege: **exatamente um evento terminal**.
Ou a resposta entrega conteúdo, ou sai UM erro dizendo por quê — nunca os dois,
nunca nenhum, e nunca o raciocínio bruto como consolo.
"""

import json
import logging

import httpx

from stream_watchdog import UpstreamStreamTimeout, aiter_bytes_watchdog

logger = logging.getLogger("gateway.reasoning")

THINK_CLOSE = "</think>"

# ordem importa só pra desempate; na prática vem um ou outro
REASONING_KEYS = ("reasoning", "reasoning_content")


def split_reasoning(text: str) -> tuple[str | None, str]:
    """Separa o bloco de raciocínio da resposta final. Modelos com thinking
    ligado não emitem a tag de abertura no texto gerado (o chat template já
    injeta "<think>\\n" no prompt) — só a de fechamento. Sem </think> na
    string, devolve (None, texto original): não há nada pra filtrar."""
    idx = text.find(THINK_CLOSE)
    if idx == -1:
        return None, text
    return text[:idx], text[idx + len(THINK_CLOSE) :].lstrip("\n")


def reasoning_text(delta: dict) -> str | None:
    """Texto de raciocínio do delta sob QUALQUER um dos dois nomes de campo.

    None quando o delta não carrega raciocínio nenhum — distinto de "" que é
    um chunk de raciocínio vazio (o vLLM manda esses no começo do stream)."""
    for key in REASONING_KEYS:
        if key in delta:
            return delta.get(key) or ""
    return None


def strip_reasoning(delta: dict) -> dict:
    """Delta sem os campos de raciocínio.

    O raciocínio é PRIVADO: nos planos filtrados ele nunca pode chegar ao
    cliente. A versão antiga deste filtro repassava a linha crua quando via
    o campo, o que teria vazado o raciocínio inteiro se a guarda funcionasse
    — não vazou só porque estava quebrada."""
    return {k: v for k, v in delta.items() if k not in REASONING_KEYS}


def entrega_visivel(delta_ou_message: dict) -> bool:
    """Carrega algo que conta como resposta ao cliente?

    `tool_calls` conta: com ENABLE_TOOL_CALLING o modelo responde chamando
    ferramenta e `content` vem vazio de propósito. Tratar isso como "resposta
    vazia" encheria de erro espúrio justamente o caminho do Claude Code, que
    é quase todo tool call."""
    return bool(
        delta_ou_message.get("content") or delta_ou_message.get("tool_calls")
    )


# Uma frase só para os dois protocolos: o mesmo desfecho descrito com palavras
# diferentes em cada endpoint viraria dois bugs de suporte distintos para a
# mesma causa. O anthropic_compat importa daqui.
EMPTY_RESPONSE_MESSAGE = (
    "a resposta terminou sem texto visível — o modelo consumiu o limite de "
    "tokens raciocinando; aumente max_tokens ou reduza o esforço de raciocínio"
)


def empty_response_error() -> dict:
    """Erro para a resposta que terminou sem UMA letra visível.

    Nem vazar o raciocínio (é privado) nem fechar em silêncio (o cliente leria
    como resposta completa e o usuário pagaria por nada). Sobra dizer o que
    houve, no formato de erro da OpenAI, que é o que os clientes deste endpoint
    sabem ler."""
    return {
        "error": {
            "message": EMPTY_RESPONSE_MESSAGE,
            "type": "empty_response",
            "code": "reasoning_only_response",
        }
    }


class ChatReasoningFilter:
    """Máquina de estados do filtro, uma por resposta.

    Dois regimes, decididos pelo que o upstream manda:

    - **parser ligado** (o delta traz "reasoning"/"reasoning_content"): o vLLM
      já separou. Suprime o campo de raciocínio e repassa o resto — streaming
      incremental preservado, que é o ponto.
    - **parser desligado** (o raciocínio vem embutido no "content" com as
      tags): buferiza até achar </think> e só então começa a repassar. Aqui o
      represamento é inevitável — não dá pra saber onde o raciocínio acaba sem
      ver a tag de fechamento.

    `thinking_esperado` diz se a request pediu raciocínio, e no regime 2 é ele
    que decide se existe buffering: sem ele não dá pra classificar o texto,
    porque o modelo não emite a tag de ABERTURA (o chat template já a injeta no
    prompt) e um texto sem </think> é indistinguível de uma resposta comum
    olhando só o conteúdo. Com thinking desligado (o gateway força isso quando
    max_tokens < MIN_MAX_TOKENS) **não se entra em raciocínio nenhum**: o
    conteúdo sai chunk a chunk desde o primeiro, e nada é descartado no fim.
    Represar aí custava o streaming inteiro dessas respostas em troca de
    proteger um raciocínio que não existe.

    `feed`/`finish` devolvem listas de bytes prontos pro wire, no mesmo padrão
    do VisibleText do demo.py."""

    def __init__(self, thinking_esperado: bool = True):
        self._pending = b""     # linha SSE incompleta entre chunks
        self._buffer_text = ""  # texto ainda não classificado (regime 2)
        # Estado inicial do regime 2. Começar SEMPRE em raciocínio represava o
        # primeiro chunk de toda resposta com thinking desligado — e nesse caso
        # não há raciocínio nenhum a esperar, então o represamento durava até o
        # fim do stream e só então o texto saía de uma vez. Com thinking
        # desligado o conteúdo é resposta desde o primeiro byte e sai na hora.
        #
        # INVARIANTE que isto estabelece: `_in_reasoning` só é True quando
        # thinking foi pedido — é o que permite ao resto do filtro tratar
        # qualquer buffer pendente como raciocínio privado, sem reconsultar a
        # request.
        self._in_reasoning = thinking_esperado
        self._parser_ativo = False
        self.entregou_visivel = False
        self.finish_reason = None
        self.usage = None
        self.done = False          # upstream mandou [DONE]
        self._erro_emitido = False

    # ---------- entrada ----------

    def feed(self, chunk: bytes) -> list[bytes]:
        self._pending += chunk
        out: list[bytes] = []
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            out.extend(self._handle_line(line))
        return out

    def finish(self, emitir_erro_vazio: bool = True) -> list[bytes]:
        """Fim do stream.

        `emitir_erro_vazio=False` quando o CHAMADOR já vai emitir um erro com a
        causa real (timeout/queda de conexão): dois erros terminais no mesmo
        stream é pior que nenhum, porque o segundo contradiz o primeiro."""
        out: list[bytes] = []
        if self._pending:
            out.extend(self._handle_line(self._pending))
            self._pending = b""
        out.extend(self._descarregar_buffer_final())
        if emitir_erro_vazio and not self.done:
            out.extend(self._fechar())
        return out

    # ---------- interno ----------

    def _descarregar_buffer_final(self) -> list[bytes]:
        """Regime 2 interrompido antes do </think>: o buffer é DESCARTADO.

        Pela invariante do __init__, chegar aqui com `_in_reasoning` implica que
        a request pediu raciocínio — e então tudo que veio antes do </think> é
        exatamente o que o filtro existe pra esconder. Repassá-lo trocaria
        resposta vazia por vazamento.

        Com thinking desligado não existe buffer a descarregar: o conteúdo já
        saiu chunk a chunk, porque `_in_reasoning` nasceu falso."""
        if not (self._in_reasoning and self._buffer_text) or self._parser_ativo:
            return []
        texto = self._buffer_text
        self._buffer_text = ""
        logger.warning(
            "descartado buffer de %d chars sem </think> (raciocínio privado)",
            len(texto),
        )
        return []

    def _handle_line(self, line: bytes) -> list[bytes]:
        stripped = line.strip()

        # linha em branco: é o separador do frame SSE. Repassada de propósito
        # mesmo quando o frame dela foi suprimido — vira keepalive e é o que
        # segura a conexão viva durante um raciocínio longo, onde nenhum byte
        # de conteúdo flui. Sem isso o cliente veria silêncio total.
        if not stripped:
            return [line + b"\n"]

        if not stripped.startswith(b"data:"):
            return [line + b"\n"]

        payload = stripped[len(b"data:") :].strip()
        if payload in (b"[DONE]", b""):
            # ANTES de repassar o [DONE]: quem lê para no [DONE], então um erro
            # emitido depois dele nunca seria visto.
            self.done = True
            return self._descarregar_buffer_final() + self._fechar() + [line + b"\n"]

        try:
            chunk = json.loads(payload)
        except Exception:
            return [line + b"\n"]
        if not isinstance(chunk, dict):
            return [line + b"\n"]

        if chunk.get("usage"):
            self.usage = chunk["usage"]

        choices = chunk.get("choices") or []
        choice0 = choices[0] if choices and isinstance(choices[0], dict) else None
        if choice0 is None:
            # chunk final de usage (choices: []) — repassa e segue
            return [line + b"\n"]

        if choice0.get("finish_reason"):
            self.finish_reason = choice0["finish_reason"]

        saida = self._classificar(line, chunk, choice0, choice0.get("delta") or {})

        # O erro precisa vir ANTES de qualquer sinal terminal — um finish_reason
        # já faz o cliente fechar o turno. E o chunk terminal VAZIO que sobrou é
        # suprimido: com o erro emitido, ele seria um segundo terminal dizendo
        # outra coisa. Fica exatamente um: o erro, e depois só o [DONE].
        if choice0.get("finish_reason") and not self.entregou_visivel:
            erro = self._fechar()
            if erro:
                return erro
        return saida

    def _classificar(self, line, chunk, choice0, delta) -> list[bytes]:
        raciocinio = reasoning_text(delta)
        if raciocinio is not None:
            self._parser_ativo = True
            self._in_reasoning = False
            return self._repassar_sem_raciocinio(chunk, choice0, delta)

        if self._parser_ativo:
            # parser já se anunciou: o content aqui é resposta limpa
            if entrega_visivel(delta):
                self.entregou_visivel = True
            return [line + b"\n"]

        return self._regime_de_tags(line, chunk, choice0, delta)

    def _repassar_sem_raciocinio(self, chunk, choice0, delta) -> list[bytes]:
        """Suprime o campo de raciocínio e repassa o que sobrar — mas só se
        sobrar alguma coisa que o cliente precise ver."""
        limpo = strip_reasoning(delta)

        # ITEM 2: content pode ter chegado ANTES do primeiro chunk de
        # raciocínio (ordem incomum, mas acontece). Descartar esse buffer
        # comeria resposta do usuário; prepende ao delta atual.
        if self._buffer_text:
            limpo = {**limpo, "content": self._buffer_text + (limpo.get("content") or "")}
            self._buffer_text = ""

        tem_entrega = entrega_visivel(limpo)
        if tem_entrega:
            self.entregou_visivel = True
        # sem entrega e sem finish_reason o chunk não carrega informação
        # nenhuma pro cliente: era só raciocínio. Suprimido por inteiro.
        if not tem_entrega and not choice0.get("finish_reason") and not chunk.get("usage"):
            return []
        novo_choice = {**choice0, "delta": limpo}
        novo = {**chunk, "choices": [novo_choice]}
        return [b"data: " + json.dumps(novo).encode() + b"\n"]

    def _regime_de_tags(self, line, chunk, choice0, delta) -> list[bytes]:
        """Raciocínio embutido no content, delimitado por </think>."""
        if not self._in_reasoning:
            if entrega_visivel(delta):
                self.entregou_visivel = True
            return [line + b"\n"]

        # tool call sem parser de raciocínio: o modelo já saiu do <think> mesmo
        # sem ter emitido </think> no texto. Não pode ficar represado.
        if delta.get("tool_calls"):
            self._in_reasoning = False
            self.entregou_visivel = True
            return [line + b"\n"]

        content = delta.get("content")
        finish_reason = choice0.get("finish_reason")
        if content:
            self._buffer_text += content

        if THINK_CLOSE in self._buffer_text:
            _, visible = split_reasoning(self._buffer_text)
            self._in_reasoning = False
            self._buffer_text = ""
            if visible or finish_reason:
                novo_delta = {**delta, "content": visible}
                novo_delta.setdefault("role", "assistant")
                novo_choice = {**choice0, "delta": novo_delta}
                if visible:
                    self.entregou_visivel = True
                return [b"data: " + json.dumps({**chunk, "choices": [novo_choice]}).encode() + b"\n"]
            return []

        if finish_reason:
            # bateu finish_reason sem nunca ver </think>. O buffer só pode ser
            # repassado se NÃO for raciocínio — ver _descarregar_buffer_final.
            saida = self._descarregar_buffer_final()
            self._in_reasoning = False
            return saida

        return []  # ainda dentro do raciocínio

    def _fechar(self) -> list[bytes]:
        """Emite o erro de resposta vazia, se for o caso. Idempotente.

        A trava é um flag próprio e não `entregou_visivel`: esse último é lido
        depois pra decidir o status do log, e reaproveitá-lo como trava
        registraria a falha como entrega bem-sucedida."""
        if self.entregou_visivel or self._erro_emitido:
            return []
        self._erro_emitido = True
        return [b"data: " + json.dumps(empty_response_error()).encode() + b"\n\n"]


def clean_message(message: dict, thinking_esperado: bool = True) -> tuple[dict, bool]:
    """Versão não-streamed: limpa a message de uma resposta completa.

    Devolve (message limpa, entregou_visivel). O caminho não-streamed sofre do
    MESMO modo de falha do streaming — com o parser ligado e o teto de tokens
    consumido pelo raciocínio, "content" vem null e a guarda antiga
    (isinstance(content, str)) simplesmente pulava, entregando resposta vazia.

    `thinking_esperado` resolve o caso simétrico e igualmente ruim: parser
    DESLIGADO, thinking ligado e nenhum </think> no texto. Aí o content inteiro
    é raciocínio, e devolvê-lo cru entrega ao cliente exatamente o que o filtro
    existe pra esconder. Com thinking desligado o mesmo texto é a resposta
    legítima e é preservado — a distinção não dá pra fazer olhando o conteúdo,
    porque o modelo não emite a tag de ABERTURA.

    `tool_calls` conta como entrega: resposta que só chama ferramenta não tem
    texto e é perfeitamente válida."""
    parser_ativo = reasoning_text(message) is not None
    limpa = strip_reasoning(message)
    content = limpa.get("content")
    if isinstance(content, str) and content:
        reasoning, visible = split_reasoning(content)
        if reasoning is not None:
            limpa["content"] = visible
        elif thinking_esperado and not parser_ativo:
            logger.warning(
                "descartado content de %d chars sem </think> (raciocínio privado)",
                len(content),
            )
            limpa["content"] = ""
    return limpa, entrega_visivel(limpa)


def clean_completion_payload(
    payload: dict, *, status_code: int, thinking_esperado: bool = True
) -> tuple[dict, bool]:
    """Limpa as choices de uma resposta NÃO-streamed. Devolve (payload, vazia).

    `status_code >= 400` nunca é classificado como vazio, e essa guarda é o
    ponto todo desta função existir separada: um corpo de erro do upstream não
    tem "choices", então sem ela ele cairia como "resposta só de raciocínio" e
    o erro REAL — status e mensagem — seria trocado por um genérico nosso,
    apagando a causa justo quando ela importa."""
    if status_code >= 400:
        return payload, False
    entregou = False
    for choice in payload.get("choices") or []:
        message = choice.get("message")
        if isinstance(message, dict):
            choice["message"], visivel = clean_message(message, thinking_esperado)
            entregou = entregou or visivel
    return payload, not entregou


async def filtered_reasoning_stream(
    upstream,
    *,
    ttft_s: float,
    idle_s: float,
    log_label: str = "",
    thinking_esperado: bool = True,
    on_close=None,
):
    """A COSTURA: watchdog + filtro + tratamento de falha + status pro log.

    Vive aqui e não no main.py justamente pra ser testável — os ramos de
    timeout e de queda de conexão são os que menos se exercitam em produção e
    os que mais custam quando estão errados.

    `on_close(status_code, usage)` é chamado no finally: o chamador usa pra
    liberar in_flight e gravar em gateway_requests. O status é o LÓGICO, não o
    do cabeçalho — o 200 já foi pro cliente junto com o header, muito antes de
    o corpo morrer, e gravar 200 pra stream morto foi o que manteve essas
    falhas invisíveis.

    Garante UM erro terminal: se o upstream falhou, sai o erro da causa real
    (timeout/queda) e o filtro é fechado sem o erro genérico de resposta
    vazia."""
    filtro = ChatReasoningFilter(thinking_esperado=thinking_esperado)
    status_code = getattr(upstream, "status_code", 200)
    erro_cliente = None
    try:
        try:
            async for raw in aiter_bytes_watchdog(
                upstream, ttft_s=ttft_s, idle_s=idle_s, log_label=log_label
            ):
                for saida in filtro.feed(raw):
                    yield saida
        except UpstreamStreamTimeout as e:
            # o upstream estourou o teto sem mandar byte. NÃO é fim de stream:
            # o cliente precisa saber que falhou, senão recebe um [DONE] limpo
            # e trata como resposta completa — foi assim que 18 de 36 requests
            # de um load test "passaram" com 200 tendo entregado nada.
            erro_cliente = {
                "message": (
                    "a máquina não entregou resposta a tempo "
                    f"({e.phase}, {e.waited:.0f}s) — "
                    "tente novamente ou reduza o tamanho do prompt"
                ),
                "type": "upstream_timeout",
                "code": "upstream_timeout",
            }
            status_code = 504
        except (httpx.HTTPError, ConnectionError, OSError):
            # a conexão com o upstream (agent/vLLM) morreu no meio do stream —
            # visto sob concorrência pesada (conexão do pool resetada pelo
            # Cloudflare enquanto ociosa).
            erro_cliente = {
                "message": (
                    "a conexão com a máquina caiu antes de a resposta terminar "
                    "— tente novamente"
                ),
                "type": "upstream_disconnect",
                "code": "upstream_disconnect",
            }
            status_code = 502

        # com erro de causa conhecida, o filtro NÃO emite o dele: um só evento
        # terminal, e ele tem que apontar a causa real
        for saida in filtro.finish(emitir_erro_vazio=erro_cliente is None):
            yield saida

        if erro_cliente is not None:
            yield b"data: " + json.dumps({"error": erro_cliente}).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"
        elif not filtro.entregou_visivel:
            # resposta sem uma letra visível (o modelo gastou o teto de tokens
            # raciocinando). O cliente já foi avisado pelo filtro; aqui o que
            # falta é o log ficar ACHÁVEL — 502 porque, do ponto de vista do
            # gateway, o upstream devolveu resposta inaproveitável. Distingue-se
            # de queda de conexão pelo tokens_out, que neste caso vem cheio.
            status_code = 502
    finally:
        await upstream.aclose()
        if on_close is not None:
            on_close(status_code, filtro.usage)
