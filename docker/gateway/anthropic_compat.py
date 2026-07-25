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
O raciocínio (<think>...</think>) dos planos em REASONING_LEAK_PLANS
(main.py) é transmitido ao vivo como content block "thinking" de
verdade, best-effort e sem signature_delta — não é o "thinking" pedido
pelo cliente (o campo "thinking" do corpo da request Anthropic não é
lido aqui, nenhum plano hoje aceita configurá-lo), é só o veículo pra
não esconder o que o vLLM já gerou (ver anthropic_sse_from_openai_stream).
NÃO coberto (fora de escopo por ora): imagens em base64 são convertidas
best-effort para image_url mas não testadas contra o vLLM; prompt
caching (cache_control) é ignorado, sem efeito no vLLM.
"""

import json
import uuid

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


def openai_to_anthropic_response(openai_resp: dict, requested_model: str) -> dict:
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
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


THINK_CLOSE = "</think>"


async def anthropic_sse_from_openai_stream(
    upstream, requested_model: str, on_done=None, filter_reasoning: bool = False
):
    """Converte o stream SSE do vLLM (chat/completions, formato OpenAI) pro
    formato de eventos da Anthropic Messages API (message_start ->
    content_block_start/delta/stop* -> message_delta -> message_stop), que
    é o que o Claude Code espera receber. `on_done` (opcional, sem args) é
    chamado no finally — usado pelo chamador pra liberar in_flight/
    concorrência, mesmo padrão de filtered_reasoning_stream em main.py.

    `filter_reasoning=True` (planos em REASONING_LEAK_PLANS, main.py)
    transmite o bloco de raciocínio (antes de </think>) como um content
    block "thinking" de verdade da Anthropic Messages API, em vez de
    empacotar no "text" junto com o resto da resposta. ANTES esse
    raciocínio ficava bufferizado inteiro e NENHUM byte chegava ao
    cliente até o fechamento de </think> (85s+ observados em tarefas
    difíceis, ver scripts/loadtest.py) — numa tarefa agêntica isso se
    repete a cada chamada de ferramenta, porque o modelo "pensa" de novo
    antes de cada decisão. Agora o raciocínio é transmitido ao vivo assim
    que sai do vLLM. Sem signature_delta (não tem como assinar como a
    Anthropic real) — seguro porque _assistant_content_to_openai_message
    já ignora blocks desconhecidos ao reconstruir histórico."""
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
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    text_block_open = False
    text_block_index = None
    thinking_block_open = False
    thinking_block_index = None
    tool_blocks: dict[int, int] = {}  # índice do tool_call OpenAI -> índice do content block Anthropic
    next_block_index = 0
    finish_reason = None
    usage = None
    pending = b""
    in_reasoning = filter_reasoning
    # cauda pendente entre chunks: só o bastante pra não cortar "</think>"
    # ao meio quando o delimitador cai na fronteira de dois chunks SSE —
    # ao contrário do buffer antigo, NÃO acumula o raciocínio inteiro (já
    # foi transmitido incrementalmente assim que chegou).
    reasoning_tail = ""
    tail_len = len(THINK_CLOSE) - 1

    try:
        try:
            async for raw in upstream.aiter_bytes():
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

                    text = delta.get("content")
                    if text and in_reasoning:
                        buf = reasoning_tail + text
                        if THINK_CLOSE in buf:
                            before, after = buf.split(THINK_CLOSE, 1)
                            if before:
                                if not thinking_block_open:
                                    thinking_block_index = next_block_index
                                    next_block_index += 1
                                    yield _sse("content_block_start", {
                                        "type": "content_block_start", "index": thinking_block_index,
                                        "content_block": {"type": "thinking", "thinking": ""},
                                    })
                                    thinking_block_open = True
                                yield _sse("content_block_delta", {
                                    "type": "content_block_delta", "index": thinking_block_index,
                                    "delta": {"type": "thinking_delta", "thinking": before},
                                })
                            if thinking_block_open:
                                yield _sse("content_block_stop", {"type": "content_block_stop", "index": thinking_block_index})
                                thinking_block_open = False
                            in_reasoning = False
                            reasoning_tail = ""
                            text = after.lstrip("\n") or None
                        else:
                            safe = buf[:-tail_len] if len(buf) > tail_len else ""
                            reasoning_tail = buf[-tail_len:] if len(buf) > tail_len else buf
                            if safe:
                                if not thinking_block_open:
                                    thinking_block_index = next_block_index
                                    next_block_index += 1
                                    yield _sse("content_block_start", {
                                        "type": "content_block_start", "index": thinking_block_index,
                                        "content_block": {"type": "thinking", "thinking": ""},
                                    })
                                    thinking_block_open = True
                                yield _sse("content_block_delta", {
                                    "type": "content_block_delta", "index": thinking_block_index,
                                    "delta": {"type": "thinking_delta", "thinking": safe},
                                })
                            text = None  # ainda dentro do raciocínio, sem texto visível ainda
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
                            if thinking_block_open:
                                yield _sse("content_block_stop", {"type": "content_block_stop", "index": thinking_block_index})
                                thinking_block_open = False
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
        except (Exception,):
            # conexão upstream caiu no meio do stream — fecha os blocks
            # abertos com o que já foi gerado, em vez de sumir sem nada
            pass

        if reasoning_tail and in_reasoning:
            # bateu o fim do stream sem nunca fechar </think> (raro, ~0-5%
            # mesmo com o piso de max_tokens) — o que já foi gerado já foi
            # transmitido ao vivo como thinking_delta; só falta soltar a
            # cauda pendente (até tail_len chars, nunca confirmada como
            # início de "</think>" real) antes de fechar o bloco.
            if not thinking_block_open:
                thinking_block_index = next_block_index
                next_block_index += 1
                yield _sse("content_block_start", {
                    "type": "content_block_start", "index": thinking_block_index,
                    "content_block": {"type": "thinking", "thinking": ""},
                })
                thinking_block_open = True
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": thinking_block_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning_tail},
            })

        if thinking_block_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": thinking_block_index})
        if text_block_open:
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": text_block_index})
        for idx in tool_blocks.values():
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})

        stop_reason = STOP_REASON_MAP.get(finish_reason, "end_turn")
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": (usage or {}).get("completion_tokens", 0)},
        })
        yield _sse("message_stop", {"type": "message_stop"})
    finally:
        if on_done:
            on_done()
