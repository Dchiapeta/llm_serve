"""
Tradução Anthropic Messages API <-> OpenAI chat/completions.

O Claude Code fala SÓ a Anthropic Messages API (POST /v1/messages) — não
tem suporte a backends OpenAI-compatíveis, ao contrário do Codex CLI (que
migrou pra Responses API, servida nativamente pelo vLLM sem tradução, ver
docker/gateway/main.py). Este módulo converte request/resposta/streaming
SSE nos dois sentidos, pra que o gateway possa expor /v1/messages sobre o
MESMO backend vLLM (chat/completions), reaproveitando todo o pipeline de
autenticação/pinning/limites/RAG que já existe pra chat/completions.

Cobertura: texto e tool use (function calling), streaming e não-streaming.
NÃO coberto (fora de escopo por ora): imagens em base64 são convertidas
best-effort para image_url mas não testadas contra o vLLM; extended
thinking (campo "thinking" da Anthropic) é ignorado — o reasoning do
vLLM, quando ligado (ENABLE_REASONING_PARSER), não tem equivalente 1:1 no
formato de "thinking blocks" da Anthropic; prompt caching (cache_control)
é ignorado, sem efeito no vLLM.
"""

import asyncio
import json
import logging
import os
import time
import uuid

import httpx

logger = logging.getLogger("gateway.anthropic")

# Kill switch do evento `error` no SSE. Quando o upstream morre ANTES de gerar
# qualquer conteúdo, este módulo passou a emitir `event: error` (evento oficial
# da Messages API, que o SDK trata como terminal) em vez de fechar a mensagem
# como se tivesse dado certo. O comportamento antigo — message_delta com
# stop_reason "end_turn" e zero conteúdo — devolvia ao cliente um turno vazio
# que AFIRMAVA sucesso: era isso que fazia o /compact do Claude Code "travar
# sem retornar nada" e o auto-compact falhar em silêncio (ele compactava, a
# request morria vazia, e a conversa seguia crescendo até estourar).
#
# "false" volta ao encerramento gracioso, sem deploy, caso algum cliente lide
# pior com o evento de erro do que com a resposta vazia.
EMIT_ERROR_EVENT = os.environ.get("ANTHROPIC_SSE_ERROR_EVENT", "true").lower() != "false"

# Kill switch do input_tokens no message_delta. A API da Anthropic emite
# input_tokens nos dois eventos (message_start e message_delta) e os SDKs
# oficiais ATRIBUEM o valor, não somam — mas o rastreador interno do Claude
# Code não é o SDK. Se em alguma versão ele somar, o contexto contado dobra e o
# cliente compacta cedo demais; aí isto vira "false" e o message_delta volta a
# mandar só output_tokens, sem precisar de deploy.
EMIT_INPUT_TOKENS_IN_DELTA = (
    os.environ.get("ANTHROPIC_EMIT_INPUT_TOKENS_IN_DELTA", "true").lower() != "false"
)

STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
}


def _anthropic_content_to_text(content) -> str:
    """Só o texto de um content Anthropic (string ou lista de blocks) —
    usado pro campo "system", que aceita os dois formatos."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _user_content_to_openai_messages(content, out_messages: list) -> None:
    """Um content de mensagem "user" da Anthropic vira 0+ mensagens OpenAI:
    cada tool_result é uma mensagem role=tool SEPARADA (OpenAI não tem o
    conceito de content block tool_result dentro de uma mensagem user);
    texto/imagem viram uma única mensagem role=user com os blocks restantes."""
    if isinstance(content, str):
        out_messages.append({"role": "user", "content": content})
        return
    if not isinstance(content, list):
        return

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            result_content = block.get("content")
            if isinstance(result_content, list):
                text = "".join(
                    b.get("text", "") for b in result_content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            elif isinstance(result_content, str):
                text = result_content
            else:
                text = json.dumps(result_content) if result_content is not None else ""
            out_messages.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": text,
            })
        elif btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})

    if not parts:
        return
    if len(parts) == 1 and parts[0]["type"] == "text":
        out_messages.append({"role": "user", "content": parts[0]["text"]})
    else:
        out_messages.append({"role": "user", "content": parts})


def _assistant_content_to_openai_message(content) -> dict:
    """O content de uma mensagem "assistant" da Anthropic vira UMA mensagem
    OpenAI: texto concatenado em "content", tool_use vira "tool_calls"."""
    if isinstance(content, str):
        return {"role": "assistant", "content": content}

    text_parts = []
    tool_calls = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                })
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _convert_tool_choice(tool_choice):
    if not isinstance(tool_choice, dict):
        return None
    t = tool_choice.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return None


def anthropic_to_openai_request(body: dict) -> tuple[dict, str]:
    """Converte um corpo da Anthropic Messages API pro formato OpenAI
    chat/completions. Devolve (corpo_openai, model_pedido_pelo_cliente): o
    model original é guardado à parte porque o pipeline de pinning
    (validate_body) reescreve body["model"] antes do proxy, e a resposta
    Anthropic precisa ecoar o model que o CLIENTE pediu — não o real
    servido internamente (Codex/Claude Code não validam contra uma lista de
    modelos do servidor, só ecoam de volta o que mandaram)."""
    requested_model = body.get("model", "")
    openai_messages = []

    system = body.get("system")
    if system:
        system_text = _anthropic_content_to_text(system)
        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant":
            openai_messages.append(_assistant_content_to_openai_message(content))
        elif role == "user":
            _user_content_to_openai_messages(content, openai_messages)

    openai_body = {
        "model": requested_model,
        "messages": openai_messages,
        "stream": bool(body.get("stream", False)),
    }
    if isinstance(body.get("max_tokens"), int):
        openai_body["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        openai_body["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        openai_body["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        openai_body["stop"] = body["stop_sequences"]

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for t in tools if isinstance(t, dict)
        ]
        tool_choice = _convert_tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            openai_body["tool_choice"] = tool_choice

    return openai_body, requested_model


def openai_to_anthropic_response(
    openai_resp: dict, requested_model: str, input_tokens_fallback: int = 0
) -> dict:
    """Resposta não-streaming: chat.completion (OpenAI) -> message (Anthropic)."""
    choice = (openai_resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content_blocks = []

    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            tool_input = json.loads(function.get("arguments") or "{}")
        except Exception:
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": function.get("name", ""),
            "input": tool_input,
        })

    stop_reason = STOP_REASON_MAP.get(choice.get("finish_reason"), "end_turn")
    usage = openai_resp.get("usage") or {}

    return {
        "id": openai_resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            # input_tokens_fallback (a estimativa do orçamento de contexto) só
            # entra se o vLLM omitiu o usage: é o número pelo qual o cliente
            # rastreia o contexto, e devolver 0 aí faz o Claude Code achar que
            # a sessão está vazia e nunca compactar.
            "input_tokens": usage.get("prompt_tokens") or input_tokens_fallback,
            "output_tokens": usage.get("completion_tokens", 0),
            # zerados mas PRESENTES, mesma razão do message_start (ver
            # anthropic_sse_from_openai_stream): há cliente que soma os três pra
            # calcular contexto usado, e em JS `undefined` propaga NaN — toda
            # comparação com NaN é falsa, então o gatilho de auto-compact nunca
            # dispararia. Ausente e 0 não são a mesma coisa.
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


THINK_CLOSE = "</think>"

# Heartbeat da Messages API. Os SDKs ignoram, mas ele mantém a conexão viva
# (Cloudflare tanto na frente do gateway quanto na frente do pod do RunPod)
# durante um prefill longo, quando não há nada de útil a mandar ainda.
PING = _sse("ping", {"type": "ping"})


async def _next_chunk(iterator):
    """`__anext__` embrulhado com sentinela em vez de StopAsyncIteration: a
    exceção não sobrevive bem a ser guardada numa Task e reinspecionada."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return None


async def anthropic_sse_from_openai_stream(
    upstream, requested_model: str, on_done=None, filter_reasoning: bool = False,
    input_tokens_estimate: int = 0, ttft_timeout_s: float = 0.0,
    idle_timeout_s: float = 0.0, ping_interval_s: float = 0.0, log_label: str = "",
):
    """Converte o stream SSE do vLLM (chat/completions, formato OpenAI) pro
    formato de eventos da Anthropic Messages API (message_start ->
    content_block_start/delta/stop* -> message_delta -> message_stop), que
    é o que o Claude Code espera receber. `on_done(usage)` (opcional) é
    chamado no finally com o último `usage` visto no stream (ou None se o
    vLLM nunca mandou) — usado pelo chamador pra liberar in_flight/
    concorrência (mesmo padrão de filtered_reasoning_stream em main.py) e
    pra logar tokens_in/tokens_out da requisição (migration 0038).

    `filter_reasoning=True` (planos em REASONING_LEAK_PLANS, main.py)
    suprime o bloco de raciocínio (antes de </think>) do "content" — mesmo
    filtro que chat/completions já aplica; sem isso o Claude Code recebia
    o raciocínio cru misturado no texto exibido nesses planos.

    `input_tokens_estimate` é o est_tokens do orçamento de contexto
    (context_budget.PromptBudget), emitido como usage.input_tokens no
    message_start. Necessário porque o vLLM só manda usage no chunk FINAL do
    stream, mas o cliente lê o input do message_start — era 0 hardcoded aqui, e
    com isso o rastreador de contexto do Claude Code nunca acumulava e o
    auto-compact nunca disparava, qualquer que fosse a janela configurada. O
    número real do vLLM corrige a estimativa no message_delta.

    `ttft_timeout_s` / `idle_timeout_s` são DUAS janelas distintas, porque medem
    coisas diferentes que o read timeout único do httpx confundia:

    - TTFT: tempo até o PRIMEIRO chunk, que é o prefill do vLLM. Num prompt de
      ~100k tokens isso passa de 60s com folga (2× A40 TP=2 em PCIe, dequant
      Marlin) — e o read de 60s do proxy_client matava exatamente a request de
      prompt máximo, que é a compactação. O prefix caching amortiza isso, mas
      não no caso que importa: a compactação reescreve o início do contexto e
      invalida o cache inteiro.
    - idle: silêncio DEPOIS que o vLLM já começou a mandar SSE. Aí sim é falha
      de verdade (pod travado, conexão zumbi) e a janela curta continua valendo.

    Zero em qualquer uma desliga aquele watchdog (default: comportamento antigo,
    quem controla o prazo é o read timeout do httpx).

    `ping_interval_s` emite `event: ping` enquanto se espera — evento oficial da
    Messages API. Serve pra manter a conexão viva através do Cloudflare durante
    um prefill longo, e pra não deixar o cliente no escuro quando o filtro de
    raciocínio represa a saída até fechar </think>."""
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            # os campos de cache vão a zero (não fazemos prompt caching — ver
            # o cabeçalho deste módulo) mas PRESENTES: shape fiel à API real, e
            # há cliente que soma os três pra calcular contexto usado, onde
            # ausente (undefined) e 0 não são a mesma coisa.
            "usage": {
                "input_tokens": input_tokens_estimate,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
            },
        },
    })

    text_block_open = False
    text_block_index = None
    tool_blocks: dict[int, int] = {}  # índice do tool_call OpenAI -> índice do content block Anthropic
    next_block_index = 0
    finish_reason = None
    usage = None
    pending = b""
    in_reasoning = filter_reasoning
    reasoning_buffer = ""

    aborted_reason = None
    # fora do try pra que o finally alcance: se o CLIENTE desconectar no meio
    # (GeneratorExit num yield), a task de leitura tem que ser cancelada, senão
    # fica rodando órfã segurando a conexão upstream
    chunk_task = None
    try:
        try:
            # Pump manual em vez de `async for`: é o que permite separar o
            # prazo do PRIMEIRO chunk (prefill) do prazo de silêncio no meio do
            # stream, e emitir ping enquanto se espera. O httpx tem um read
            # timeout só, que confunde as duas coisas.
            #
            # NÃO trocar por asyncio.wait_for: ele CANCELA o __anext__ no
            # timeout, o que injeta CancelledError dentro do gerador do httpx e
            # deixa a conexão inutilizável — o chunk seguinte quebra. Aqui a
            # task é deixada pendente de propósito e só é cancelada quando já
            # estamos abandonando o stream.
            iterator = upstream.aiter_bytes().__aiter__()
            first_chunk_seen = False
            waiting_since = time.monotonic()
            while True:
                if chunk_task is None:
                    chunk_task = asyncio.ensure_future(_next_chunk(iterator))
                budget = idle_timeout_s if first_chunk_seen else ttft_timeout_s
                waits = [t for t in (ping_interval_s, budget) if t > 0]
                done, _ = await asyncio.wait(
                    {chunk_task}, timeout=min(waits) if waits else None
                )
                if not done:
                    waited = time.monotonic() - waiting_since
                    if budget and waited > budget:
                        chunk_task.cancel()
                        aborted_reason = (
                            "prefill" if not first_chunk_seen else "silêncio"
                        )
                        logger.warning(
                            "anthropic stream: %s sem chunk do upstream por %.0fs "
                            "(teto %.0fs) em %s",
                            aborted_reason, waited, budget, log_label or "?",
                        )
                        break
                    if ping_interval_s > 0:
                        yield PING
                    continue
                raw = chunk_task.result()
                chunk_task = None
                if raw is None:
                    break
                if not first_chunk_seen:
                    first_chunk_seen = True
                    logger.info(
                        "anthropic stream: TTFT %.1fs em %s",
                        time.monotonic() - waiting_since, log_label or "?",
                    )
                waiting_since = time.monotonic()
                pending += raw
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    stripped = line.strip()
                    if not stripped.startswith(b"data:"):
                        continue
                    payload_raw = stripped[len(b"data:"):].strip()
                    if payload_raw == b"[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload_raw)
                    except Exception:
                        continue

                    if chunk.get("usage"):
                        usage = chunk["usage"]

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice0 = choices[0]
                    delta = choice0.get("delta") or {}
                    if choice0.get("finish_reason"):
                        finish_reason = choice0["finish_reason"]

                    # vLLM com --reasoning-parser (ENABLE_REASONING_PARSER, ver
                    # docker/entrypoint.sh) já separa o raciocínio em
                    # "reasoning_content" — nesse caso "content" NUNCA traz um
                    # </think> pra fechar o buffer abaixo, e a resposta inteira
                    # sairia de uma vez só no fallback de fim de stream (o
                    # Claude Code perde streaming incremental por completo).
                    # Detectar aqui e desligar o filtro nesta resposta.
                    # PAR a manter sincronizado: o mesmo branch em
                    # main.py:filtered_reasoning_stream (REASONING_LEAK_PLANS) —
                    # são duas cópias do mesmo filtro, uma por protocolo.
                    if "reasoning_content" in delta:
                        in_reasoning = False
                        # o buffer pode já ter texto se o vLLM mandou "content"
                        # antes do primeiro chunk de raciocínio; descartá-lo
                        # comeria resposta do usuário. Prepende ao delta atual.
                        if reasoning_buffer:
                            delta = {
                                **delta,
                                "content": reasoning_buffer + (delta.get("content") or ""),
                            }
                            reasoning_buffer = ""

                    text = delta.get("content")
                    if text and in_reasoning:
                        reasoning_buffer += text
                        if THINK_CLOSE in reasoning_buffer:
                            _, visible = reasoning_buffer.split(THINK_CLOSE, 1)
                            in_reasoning = False
                            reasoning_buffer = ""
                            text = visible.lstrip("\n") or None
                        else:
                            text = None  # ainda represado, nada a emitir agora
                    if text:
                        if not text_block_open:
                            text_block_index = next_block_index
                            next_block_index += 1
                            yield _sse("content_block_start", {
                                "type": "content_block_start", "index": text_block_index,
                                "content_block": {"type": "text", "text": ""},
                            })
                            text_block_open = True
                        yield _sse("content_block_delta", {
                            "type": "content_block_delta", "index": text_block_index,
                            "delta": {"type": "text_delta", "text": text},
                        })

                    for tc in delta.get("tool_calls") or []:
                        oi = tc.get("index", 0)
                        if oi not in tool_blocks:
                            if text_block_open:
                                yield _sse("content_block_stop", {"type": "content_block_stop", "index": text_block_index})
                                text_block_open = False
                            tool_blocks[oi] = next_block_index
                            next_block_index += 1
                            function = tc.get("function") or {}
                            yield _sse("content_block_start", {
                                "type": "content_block_start", "index": tool_blocks[oi],
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                                    "name": function.get("name", ""),
                                    "input": {},
                                },
                            })
                        function = tc.get("function") or {}
                        args_fragment = function.get("arguments")
                        if args_fragment:
                            yield _sse("content_block_delta", {
                                "type": "content_block_delta", "index": tool_blocks[oi],
                                "delta": {"type": "input_json_delta", "partial_json": args_fragment},
                            })
        except (httpx.HTTPError, ConnectionError, OSError) as e:
            # conexão upstream caiu no meio do stream — fecha os blocks
            # abertos com o que já foi gerado, em vez de sumir sem nada.
            #
            # Específico em vez de `except Exception` nu por dois motivos: um
            # CancelledError de cliente que desconectou não é falha de API (e
            # nem é Exception), e engolir tudo em silêncio foi o que escondeu
            # este bug — sem esta linha de log, um stream que morre no prefill
            # some sem deixar rastro em lugar nenhum.
            aborted_reason = aborted_reason or "upstream"
            logger.warning(
                "anthropic stream: upstream caiu em %s (%s: %s)",
                log_label or "?", type(e).__name__, e,
            )

        if aborted_reason and not text_block_open and not tool_blocks and not reasoning_buffer:
            # Morreu sem gerar UMA palavra. Fechar a mensagem normalmente aqui
            # produzia um turno vazio com stop_reason "end_turn" e HTTP 200 —
            # uma resposta que AFIRMA sucesso. Era o que fazia o /compact do
            # Claude Code "travar sem retornar nada": ele recebia um resumo
            # vazio e válido, e a conversa seguia crescendo até estourar.
            #
            # `error` é evento oficial da Messages API e o SDK o trata como
            # terminal, então a falha aparece pro usuário como falha.
            if EMIT_ERROR_EVENT:
                yield _sse("error", {
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": (
                            "a máquina não começou a responder a tempo — prompt "
                            "muito grande para o prefill ou pod sob carga. Tente "
                            "de novo; se persistir, reduza o contexto da sessão."
                        ),
                    },
                })
                return

        if in_reasoning and reasoning_buffer:
            # bateu o fim do stream sem nunca fechar </think> — devolve o
            # que foi acumulado em vez de descartar a resposta inteira
            # (mesmo fallback de filtered_reasoning_stream em main.py)
            if not text_block_open:
                text_block_index = next_block_index
                next_block_index += 1
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
                text_block_open = True
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": text_block_index,
                "delta": {"type": "text_delta", "text": reasoning_buffer},
            })

        if text_block_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": text_block_index})
        for idx in tool_blocks.values():
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

        # Chegou aqui com aborted_reason = havia conteúdo parcial (o caso sem
        # nada nenhum já retornou acima). "end_turn" seria mentira: a resposta
        # foi cortada, não concluída.
        if aborted_reason and finish_reason is None:
            stop_reason = "max_tokens"
        else:
            stop_reason = STOP_REASON_MAP.get(finish_reason, "end_turn")
        delta_usage = {
            "output_tokens": (usage or {}).get("completion_tokens", 0),
            # presentes e zerados pelo mesmo motivo do message_start: cliente
            # que soma os três recebe NaN se algum vier undefined, e NaN nunca
            # ultrapassa o limiar de compactação
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        if EMIT_INPUT_TOKENS_IN_DELTA:
            # corrige a estimativa do message_start com a contagem real do
            # vLLM (que só chega neste ponto do stream); sem usage no stream,
            # repete a estimativa em vez de deixar o cliente sem número
            delta_usage["input_tokens"] = (
                (usage or {}).get("prompt_tokens") or input_tokens_estimate
            )
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": delta_usage,
        })
        yield _sse("message_stop", {"type": "message_stop"})
    finally:
        # ordem importa: cancelar a leitura ANTES de fechar o upstream, senão o
        # aclose disputa com um __anext__ ainda em voo
        if chunk_task is not None:
            chunk_task.cancel()
        # o httpx só devolve a conexão ao pool quando o stream é consumido até
        # o fim; abandonar no meio (timeout, cliente desconectou) sem fechar
        # queima um slot de max_connections pra sempre. O caminho OpenAI
        # (main.py:filtered_reasoning_stream) sempre fechou, este não fechava.
        try:
            await upstream.aclose()
        except Exception:
            pass
        if on_done:
            on_done(usage)
