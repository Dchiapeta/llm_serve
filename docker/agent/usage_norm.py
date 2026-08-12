"""Normalização do bloco `usage` dos protocolos generativos do vLLM.

Funções PURAS, sem env/rede/FastAPI — importáveis pelos testes sem subir o
agent (mesma disciplina de proxy_policy.py).

CÓPIA SINCRONIZADA: docker/gateway/usage_norm.py é o mesmo arquivo. São duas
imagens diferentes (gateway roda em CPU no Railway, agent vive dentro do pod)
e os dois lados contabilizam tokens em tabelas diferentes — gateway_requests
no gateway, usage_metrics via /admin/metrics do agent. Mesma duplicação
consciente de ALLOWED_V1 e do filtro de modelos `acct-*`. Mudança aqui tem
que ser espelhada lá.

---------------------------------------------------------------------------
Por que existe
---------------------------------------------------------------------------
O vLLM serve dois protocolos generativos com contabilidade INCOMPATÍVEL:

    chat/completions, completions   usage na raiz da resposta (ou do chunk
                                    final do SSE), campos prompt_tokens /
                                    completion_tokens
    responses (Codex CLI)           usage DENTRO de `response`, e só no
                                    evento final "response.completed",
                                    campos input_tokens / output_tokens
                                    (vllm/entrypoints/openai/responses/
                                    serving.py, ResponseUsage)

Todo o código que grava tokens nasceu só com o primeiro formato e falhava em
SILÊNCIO no segundo — de dois jeitos que se somavam:

  1. `parsed.get("usage")` devolvia None no evento do Responses, porque o
     dict está aninhado em `response`. Pior: o atalho `b'"usage"' in line`
     casava (a substring existe, aninhada), então a linha era parseada e
     descartada como se não tivesse contagem nenhuma.
  2. no caminho não-streaming, onde o dict chegava inteiro, o
     `.get("prompt_tokens")` não existe nele — vira NULL de novo.

Sintoma medido em 11/08/2026, quando o primeiro cliente entrou de Codex CLI:
81 de 81 requisições `responses` com tokens_in/tokens_out NULL em
gateway_requests, usage_metrics com requests > 0 e tokens zerados (é o que o
dashboard soma), quota diária de tokens (check_token_quota) cega ao uso todo
e usage_class travada em "low" pra qualquer stack que só fale Responses.

Normaliza PARA o formato chat, não para um terceiro formato próprio, porque é
o que o resto do sistema já consome: as colunas tokens_in/tokens_out, o
usage_summary do agent e o _log_estimate_drift do gateway.
"""

import json


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) else None


def normalize_usage(usage: dict | None) -> dict | None:
    """Traduz um bloco de usage para o formato chat (prompt_tokens /
    completion_tokens / total_tokens + prompt_tokens_details.cached_tokens).

    IDEMPOTENTE: um bloco já no formato chat volta igual. É o que permite
    chamar isto no ponto que grava no banco (log_gateway_request) sem auditar
    cada um dos chamadores.

    Devolve None — e não um dict de zeros — quando não há contagem NENHUMA
    reconhecível. A diferença importa: os eventos intermediários do Responses
    (`response.created`, `response.in_progress`) carregam `"usage": null`, e um
    dict de zeros vindo deles sobrescreveria o usage real do evento final."""
    if not isinstance(usage, dict):
        return None

    prompt = _int_or_none(usage.get("prompt_tokens"))
    completion = _int_or_none(usage.get("completion_tokens"))
    cached_from = "prompt_tokens_details"
    if prompt is None and completion is None:
        prompt = _int_or_none(usage.get("input_tokens"))
        completion = _int_or_none(usage.get("output_tokens"))
        cached_from = "input_tokens_details"
    if prompt is None and completion is None:
        return None

    normalized = {
        "prompt_tokens": prompt or 0,
        "completion_tokens": completion or 0,
    }
    total = _int_or_none(usage.get("total_tokens"))
    normalized["total_tokens"] = (
        total
        if total is not None
        else normalized["prompt_tokens"] + normalized["completion_tokens"]
    )

    # cached_tokens só existe com --enable-prompt-tokens-details no vLLM; sem a
    # flag o campo não vem e a chave fica ausente (não zero), que é o que o
    # cached_tokens_of do agent já trata como "sem informação".
    details = usage.get(cached_from)
    if isinstance(details, dict):
        cached = _int_or_none(details.get("cached_tokens"))
        if cached is not None:
            normalized["prompt_tokens_details"] = {"cached_tokens": cached}
    return normalized


def usage_from_event(parsed) -> dict | None:
    """Usage de um objeto JSON já parseado: um evento do SSE ou uma resposta
    inteira não-streamed. Cobre os dois protocolos, normalizado."""
    if not isinstance(parsed, dict):
        return None
    found = normalize_usage(parsed.get("usage"))
    if found:
        return found
    # Responses API: `response` é o snapshot da resposta, e só o do evento
    # final ("response.completed") tem usage preenchido.
    response = parsed.get("response")
    if isinstance(response, dict):
        return normalize_usage(response.get("usage"))
    return None


def usage_from_sse_line(line: bytes) -> dict | None:
    """Mesmo trabalho a partir de uma linha CRUA do SSE. None para tudo que
    não seja um `data:` com contagem — `[DONE]`, deltas de conteúdo, linhas
    de comentário e JSON quebrado."""
    if b'"usage"' not in line:
        # atalho do caminho quente: num stream longo a esmagadora maioria das
        # linhas é delta de conteúdo, e json.loads em cada uma custaria caro
        return None
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return None
    data = stripped[len(b"data:") :].strip()
    if data in (b"[DONE]", b""):
        return None
    try:
        parsed = json.loads(data)
    except Exception:
        return None
    return usage_from_event(parsed)


class SseUsageScanner:
    """Acumulador de um stream SSE que devolve o último usage visto.

    Existe porque a linha que carrega o usage pode ser ENORME — no protocolo
    Responses o evento final traz o snapshot inteiro da resposta, todo o texto
    gerado incluído — e os chunks que chegam da rede não respeitam fronteira de
    linha. Guardar "os últimos N bytes do stream" (o que o agent fazia, com
    N=16 KiB) corta o JSON no meio e perde a contagem em silêncio. O scanner
    guarda no máximo UMA linha incompleta e resolve cada linha ao completá-la.

        scanner = SseUsageScanner()
        async for chunk in upstream.aiter_bytes():
            yield chunk
            scanner.feed(chunk)
        usage = scanner.finish()
    """

    __slots__ = ("_pending", "_usage")

    def __init__(self) -> None:
        self._pending = b""
        self._usage = None

    def feed(self, chunk: bytes) -> None:
        self._pending += chunk
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            found = usage_from_sse_line(line)
            if found:
                self._usage = found

    def finish(self) -> dict | None:
        """Fecha o stream e devolve o usage. Examina a linha pendente, que pode
        não ter vindo com newline no fim — e no Responses é justamente a última
        linha que carrega a contagem. Idempotente."""
        found = usage_from_sse_line(self._pending)
        if found:
            self._usage = found
        self._pending = b""
        return self._usage
