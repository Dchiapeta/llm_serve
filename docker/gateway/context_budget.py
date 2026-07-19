"""Orçamento de contexto por request: clamp de max_tokens à janela real do
modelo (machines.max_model_len, migration 0031) e rejeição clara quando nem o
prompt cabe.

Motivação: o gateway aplicava só piso/teto fixos (MIN/MAX_MAX_TOKENS) sem
conhecer o --max-model-len do vLLM que vai atender — um cliente de prompt
grande (Claude Code manda ~26k tokens só de system+tools) recebia o 400 cru
do vLLM ("This model's maximum context length is 16384 tokens..."). Aqui o
gateway reserva espaço pro prompt estimado e entrega o restante da janela como
teto de saída; se nem o prompt couber, devolve um erro acionável no formato do
cliente (Anthropic ou OpenAI, via exception handler no main.py).

Módulo separado do main.py de propósito: funções puras, importáveis pelos
testes sem as env vars obrigatórias (SUPABASE_URL etc.) do main.
"""

import json
import math
import os

from fastapi import HTTPException

# Margem sobre a estimativa de prompt: a heurística ~4 chars/token subestima
# texto denso (código tokeniza mais "caro"); o json.dumps já infla um pouco
# (aspas/escapes), mas não o bastante pra dispensar folga.
CONTEXT_SAFETY_FACTOR = float(os.environ.get("CONTEXT_SAFETY_FACTOR", "1.2"))
# Tokens do chat template (marcadores de role, BOS/EOS) que não aparecem no
# corpo JSON mas ocupam janela no vLLM.
CONTEXT_TEMPLATE_OVERHEAD = int(os.environ.get("CONTEXT_TEMPLATE_OVERHEAD", "200"))
# Abaixo disso não há resposta útil possível — melhor rejeitar com instrução
# de compactar do que devolver meia dúzia de tokens truncados.
MIN_VIABLE_COMPLETION_TOKENS = int(os.environ.get("MIN_VIABLE_COMPLETION_TOKENS", "256"))


class ContextWindowExceeded(HTTPException):
    """Prompt não deixa espaço viável de resposta na janela do modelo.

    Subclasse de HTTPException de propósito: os `except HTTPException` dos
    call sites (release_flight) continuam funcionando, e o exception handler
    registrado no main.py (resolvido por MRO, vence o default do FastAPI)
    formata o corpo no shape do cliente — Anthropic em /v1/messages*, OpenAI
    no resto."""

    def __init__(self, message: str):
        super().__init__(status_code=400, detail=message)


def anthropic_error_body(message: str) -> dict:
    """Shape de erro da Anthropic Messages API (o que o Claude Code exibe)."""
    return {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }


def openai_error_body(message: str) -> dict:
    """Shape de erro OpenAI, com o code canônico de estouro de contexto."""
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
    }


def _strip_images(messages: list) -> list:
    """Remove partes image_url do cálculo — base64 a ~4 chars/token
    superestimaria absurdamente (imagem não tokeniza como texto)."""
    out = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            m = {
                **m,
                "content": [
                    part
                    for part in m["content"]
                    if not (isinstance(part, dict) and part.get("type") == "image_url")
                ],
            }
        out.append(m)
    return out


def estimate_prompt_tokens(messages=None, tools=None, extra_texts=()) -> int:
    """Heurística ~4 chars/token sobre o corpo já em formato OpenAI. Conta
    também as tools (nos clientes agênticos são a maior fatia do prompt — o
    count_tokens antigo ignorava e o Claude Code compactava tarde demais) e
    textos avulsos ("prompt" do /v1/completions, "instructions"/"input" da
    Responses API)."""
    # ensure_ascii=False: com o default, cada caractere acentuado vira ç
    # (6 chars) e texto em português — o público do produto — é superestimado
    # em ~2x, clampando saída de prompts que cabem e fazendo o count_tokens
    # mandar o Claude Code compactar na metade útil da janela
    chars = 0
    if isinstance(messages, list):
        chars += len(json.dumps(_strip_images(messages), ensure_ascii=False))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    for text in extra_texts:
        if isinstance(text, str):
            chars += len(text)
    return max(1, chars // 4)


def apply_context_budget(
    body_json: dict, machine: dict, field: str = "max_tokens", est_tokens: int = 0
) -> None:
    """Clampa body_json[field] ao que sobra da janela após reservar o prompt.

    Roda DEPOIS do piso/teto fixos (MIN/MAX_MAX_TOKENS) e pode ficar abaixo do
    piso — senão o piso re-inflaria o valor e o 400 do vLLM voltaria.
    machines.max_model_len ausente (template sem --max-model-len, pod anterior
    à migration 0031) → no-op: o vLLM usa a janela nativa do config do modelo,
    que o gateway não tem como conhecer; chutar seria pior que não clampar."""
    max_model_len = machine.get("max_model_len")
    if not isinstance(max_model_len, int) or max_model_len <= 0:
        return
    reserve = math.ceil(est_tokens * CONTEXT_SAFETY_FACTOR) + CONTEXT_TEMPLATE_OVERHEAD
    budget = max_model_len - reserve
    if budget < MIN_VIABLE_COMPLETION_TOKENS:
        raise ContextWindowExceeded(
            f"o prompt (estimado em ~{est_tokens} tokens) excede a janela de "
            f"contexto de {max_model_len} tokens do seu plano; reduza o histórico "
            "da conversa ou os arquivos anexados (no Claude Code, use /compact) "
            "e tente novamente"
        )
    current = body_json.get(field)
    if isinstance(current, int) and current > budget:
        body_json[field] = budget
