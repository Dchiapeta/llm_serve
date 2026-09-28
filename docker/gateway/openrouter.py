"""Repasse de inferência para o OpenRouter.

Decisão de 28/09/2026: por um período, a Stac deixa de subir máquinas próprias
para os modelos que o cliente pedir e repassa a request ao OpenRouter. Quem
decide QUAIS modelos são aceitos é a página "Modelos (OpenRouter)" do painel
(tabela openrouter_models, migration 0071); o cliente escolhe um deles pelo
campo `model` da request. Dois interruptores em system_settings governam o
todo: `openrouter_enabled` (este repasse) e `machines_enabled` (o caminho
antigo, de máquinas no RunPod).

A regra de destino, avaliada depois de autenticação, rate limit e cota:

    modelo pedido está na allowlist (e o repasse está ligado)  → OpenRouter
    senão, máquinas ligadas                                    → máquina (como sempre foi)
    senão                                                      → erro com a lista aceita

O protocolo do cliente é repassado no MESMO protocolo do lado de lá, sem
tradução: chat/completions → chat/completions, responses → responses,
/v1/messages (Anthropic) → /api/v1/messages. Traduzir Anthropic → OpenAI, como
o caminho de máquina faz para o vLLM, jogaria fora assinaturas de thinking e
cache_control que modelos Claude servidos pelo OpenRouter entendem.

Funções PURAS: sem env, rede nem FastAPI — importáveis pelos testes sem subir
o gateway (mesma disciplina de usage_norm.py e context_budget.py). O I/O
(cliente httpx, cache do catálogo, log em gateway_requests) mora em main.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterator

from usage_norm import normalize_usage

TEXT_KIND = "text"
IMAGE_KIND = "image"

# /v1/<path> do catch-all que podem ir para o OpenRouter → path de lá. Os que
# ficam de fora (embeddings) seguem só para máquina.
TEXT_PATHS: dict[str, str] = {
    "chat/completions": "chat/completions",
    "completions": "completions",
    "responses": "responses",
}

# Heartbeat enquanto o upstream está calado. O OpenRouter manda o próprio
# (": OPENROUTER PROCESSING"), mas não há garantia documentada dele em todo
# endpoint — e é justamente o silêncio que faz o proxy na frente do gateway
# derrubar a conexão ("check your network" do Claude Code).
OPENAI_SSE_PING = b": ping\n\n"
ANTHROPIC_SSE_PING = b'event: ping\ndata: {"type": "ping"}\n\n'

# Campos do corpo de imagem repassados como vieram. `prompt` e `model` são
# decididos pelo gateway; o resto (response_format, user, steps do FLUX...) não
# existe no /api/v1/images e é descartado.
IMAGE_FIELDS = (
    "n", "size", "quality", "output_format", "background", "seed",
    "aspect_ratio", "resolution", "output_compression",
)
_IMAGE_INT_FIELDS = ("n", "seed", "output_compression")

# Campos que só o vLLM entende. O OpenRouter pode recusar o corpo inteiro por
# causa deles (a validação de lá é estrita em alguns provedores).
_VLLM_ONLY_FIELDS = ("chat_template_kwargs", "cache_salt")


# ---------- catálogo ----------


def requested_model(body: bytes | dict | None) -> str | None:
    """`model` pedido pelo cliente, ou None se não há um legível."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if not isinstance(body, dict):
        return None
    model = body.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def pick_slug(model: str | None, kind: str, catalog: dict[str, str]) -> str | None:
    """Slug do OpenRouter para o `model` pedido, ou None se ele não está na
    allowlist (ou está, mas é de outro tipo — um modelo de imagem não atende
    chat). `catalog` é {slug: kind} só com os modelos habilitados.

    Casamento exato: o cliente manda o slug do OpenRouter como está no painel.
    Alias próprio foi descartado de propósito — é mais uma tabela para manter
    num arranjo que é temporário."""
    if not model:
        return None
    return model if catalog.get(model) == kind else None


def accepted_models(catalog: dict[str, str], kind: str) -> list[str]:
    return sorted(slug for slug, k in catalog.items() if k == kind)


def model_list_entries(slugs: list[str]) -> list[dict]:
    """Entradas no formato do GET /v1/models da OpenAI. `owned_by` fica
    "stac": o cliente contratou a Stac, não o provedor por trás dela."""
    return [
        {"id": slug, "object": "model", "created": 0, "owned_by": "stac"}
        for slug in slugs
    ]


def unavailable_detail(model: str | None, accepted: list[str]) -> str:
    """Mensagem do erro quando o modelo pedido não tem destino nenhum."""
    pedido = f"o modelo '{model}'" if model else "o modelo pedido"
    if not accepted:
        return f"{pedido} não está disponível no momento"
    return f"{pedido} não está disponível. Modelos aceitos: {', '.join(accepted)}"


# ---------- corpo ----------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def prepare_openai_body(body: dict, slug: str, max_tokens_cap: int) -> dict:
    """Corpo de chat/completions, completions ou responses pronto para o
    OpenRouter. Trava o modelo, limita o teto de saída (o custo aqui é por
    token, não por máquina) e força n=1 (n>1 multiplica o custo sem valor para
    o caso de uso, mesmo motivo do validate_body)."""
    body["model"] = slug
    if max_tokens_cap > 0:
        for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            value = body.get(field)
            if _is_int(value) and value > max_tokens_cap:
                body[field] = max_tokens_cap
    if _is_int(body.get("n")):
        body["n"] = 1
    for field in _VLLM_ONLY_FIELDS:
        body.pop(field, None)
    return body


def prepare_anthropic_body(body: dict, slug: str, max_tokens_cap: int) -> dict:
    """Corpo do /v1/messages pronto para o /api/v1/messages do OpenRouter.

    Ao reduzir max_tokens, o orçamento de thinking tem que caber embaixo dele:
    a API Anthropic recusa `budget_tokens >= max_tokens` com 400, e o Claude
    Code manda orçamentos de ~32K. Abaixo do mínimo aceito (1024) o thinking sai
    inteiro — melhor responder sem raciocínio do que não responder."""
    body["model"] = slug
    max_tokens = body.get("max_tokens")
    if max_tokens_cap > 0 and _is_int(max_tokens) and max_tokens > max_tokens_cap:
        max_tokens = body["max_tokens"] = max_tokens_cap
    thinking = body.get("thinking")
    if (
        isinstance(thinking, dict)
        and _is_int(thinking.get("budget_tokens"))
        and _is_int(max_tokens)
        and thinking["budget_tokens"] >= max_tokens
    ):
        if max_tokens - 1 >= 1024:
            thinking["budget_tokens"] = max_tokens - 1
        else:
            body.pop("thinking", None)
    return body


def anthropic_system_text(system: Any) -> str:
    """Texto do `system` Anthropic (string ou lista de blocks)."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(
            block["text"]
            for block in system
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def data_url(content_type: str | None, raw: bytes) -> str:
    kind = (content_type or "").split(";")[0].strip() or "image/png"
    return f"data:{kind};base64,{base64.b64encode(raw).decode()}"


def image_body(
    fields: dict,
    *,
    slug: str,
    prompt: str | None,
    references: list[str] | None = None,
    max_images: int = 4,
) -> dict:
    """Corpo do POST /api/v1/images a partir do corpo OpenAI do cliente.

    `fields` vem do JSON do generations ou do form do edits — no form tudo
    chega como string, daí a coerção dos inteiros. Edição de imagem no
    OpenRouter não é uma rota própria: é o mesmo endpoint com as referências
    em `input_references` (URLs ou data URLs)."""
    out: dict[str, Any] = {"model": slug}
    if prompt is not None:
        out["prompt"] = prompt
    for field in IMAGE_FIELDS:
        value = fields.get(field)
        if value is None or value == "":
            continue
        if field in _IMAGE_INT_FIELDS and isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                continue
        out[field] = value
    if _is_int(out.get("n")):
        out["n"] = max(1, min(out["n"], max_images))
    if references:
        out["input_references"] = [
            {"type": "image_url", "image_url": {"url": url}} for url in references
        ]
    return out


# ---------- resposta ----------


def error_of(payload: Any) -> str | None:
    """Mensagem de um `error` no corpo. O OpenRouter pode devolver 200 com só
    `error` e nenhum `choices` (erro depois de a request ser aceita)."""
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        message = err.get("message")
        return message if isinstance(message, str) and message else "erro no provedor"
    if isinstance(err, str) and err:
        return err
    return None


def hides_provider_error(status_code: int) -> bool:
    """Erros que são problema da CONTA da Stac no OpenRouter (chave inválida,
    crédito esgotado), não da request do cliente. Não vazam: o cliente recebe
    um 503 genérico e o log do gateway fica com o motivo real."""
    return status_code in (401, 402)


class UsageCostScanner:
    """Tokens e custo de uma resposta do OpenRouter, em qualquer dos três
    protocolos repassados:

        chat/completions   `usage` no chunk final (ou na raiz), com `cost`
        responses          `response.usage` no response.completed
        messages           `message.usage` no message_start (input) e `usage`
                           no message_delta (output, e o custo)

    Os blocos são MESCLADOS campo a campo, o mais recente ganhando: no
    Anthropic nenhum evento sozinho tem as duas contagens. `feed` recebe os
    bytes crus do SSE (guarda no máximo uma linha incompleta, como o
    SseUsageScanner); `absorb` recebe um JSON já parseado (não-streaming).

    `error` guarda a mensagem de um erro no MEIO do stream: depois dos
    cabeçalhos o OpenRouter não muda mais o status (fica 200) e manda o erro
    como um evento com `error` no topo. Sem isto o log gravaria sucesso."""

    __slots__ = ("_pending", "_raw", "error")

    def __init__(self) -> None:
        self._pending = b""
        self._raw: dict[str, Any] = {}
        self.error: str | None = None

    def feed(self, chunk: bytes) -> None:
        self._pending += chunk
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            self._line(line)

    def _line(self, line: bytes) -> None:
        if b'"usage"' not in line and b'"error"' not in line:
            return  # atalho do caminho quente: deltas de conteúdo
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            return
        data = stripped[len(b"data:"):].strip()
        if data in (b"", b"[DONE]"):
            return
        try:
            self.absorb(json.loads(data))
        except Exception:
            return

    def absorb(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            return
        message = error_of(parsed)
        if message:
            self.error = message
        blocks = [parsed.get("usage")]
        for holder in ("response", "message"):
            inner = parsed.get(holder)
            if isinstance(inner, dict):
                blocks.append(inner.get("usage"))
        for block in blocks:
            if isinstance(block, dict):
                self._raw.update({k: v for k, v in block.items() if v is not None})

    def finish(self) -> tuple[dict | None, float | None]:
        """(usage no formato chat, custo em USD). Idempotente."""
        self._line(self._pending)
        self._pending = b""
        raw = self._raw
        cost = raw.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        if "prompt_tokens" in raw or "completion_tokens" in raw:
            return normalize_usage(raw), cost
        # Anthropic conta o cache FORA de input_tokens; o que a Stac registra
        # como tokens de entrada é o prompt inteiro que o modelo processou.
        if "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
            raw = dict(raw)
            raw["input_tokens"] = sum(
                v for v in (
                    raw.get("input_tokens"),
                    raw.get("cache_read_input_tokens"),
                    raw.get("cache_creation_input_tokens"),
                ) if _is_int(v)
            )
        return normalize_usage(raw), cost


def usage_and_cost(raw: bytes) -> tuple[dict | None, float | None]:
    """Mesmo trabalho do scanner para um corpo não-streamed inteiro."""
    scanner = UsageCostScanner()
    try:
        scanner.absorb(json.loads(raw))
    except Exception:
        pass
    return scanner.finish()


# Campos do usage que dizem quanto a Stac PAGA ao OpenRouter. Saem da resposta
# antes de chegar ao cliente (o valor já foi para gateway_requests.cost_usd):
# repassar o corpo byte a byte expunha o custo do fornecedor em toda resposta.
_COST_KEYS = ("cost", "cost_details", "is_byok")


def strip_cost(parsed: Any) -> bool:
    """Remove os campos de custo dos blocos de usage de `parsed`, in place.
    True se algo foi removido."""
    if not isinstance(parsed, dict):
        return False
    changed = False
    holders = [parsed]
    for key in ("response", "message"):
        if isinstance(parsed.get(key), dict):
            holders.append(parsed[key])
    for holder in holders:
        usage = holder.get("usage")
        if isinstance(usage, dict):
            for key in _COST_KEYS:
                if key in usage:
                    del usage[key]
                    changed = True
    return changed


def strip_cost_body(raw: bytes) -> bytes:
    """strip_cost para um corpo JSON inteiro. Corpo sem custo volta intacto."""
    if b'"cost' not in raw and b'"is_byok"' not in raw:
        return raw
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    if not strip_cost(parsed):
        return raw
    return json.dumps(parsed, ensure_ascii=False).encode()


def _strip_cost_line(line: bytes) -> bytes:
    if b'"cost' not in line and b'"is_byok"' not in line:
        return line
    cr = line.endswith(b"\r")
    body = line[:-1] if cr else line
    if not body.startswith(b"data:"):
        return line
    try:
        parsed = json.loads(body[len(b"data:"):])
    except Exception:
        return line
    if not strip_cost(parsed):
        return line
    return b"data: " + json.dumps(parsed, ensure_ascii=False).encode() + (b"\r" if cr else b"")


class CostStripper:
    """strip_cost num SSE, linha a linha. Segura só a linha incompleta do fim
    do chunk (os chunks do SSE quase sempre terminam em fronteira de linha,
    então na prática não atrasa nada)."""

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending = b""

    def feed(self, chunk: bytes) -> bytes:
        self._pending += chunk
        if b"\n" not in self._pending:
            return b""
        head, _, self._pending = self._pending.rpartition(b"\n")
        return b"\n".join(_strip_cost_line(line) for line in head.split(b"\n")) + b"\n"

    def flush(self) -> bytes:
        out, self._pending = _strip_cost_line(self._pending), b""
        return out


def stream_error_frame(message: str, code: str, *, anthropic: bool) -> bytes:
    """Frame de erro no meio de um SSE já iniciado (o 200 foi junto com o
    cabeçalho, então o erro só cabe no corpo)."""
    if anthropic:
        body = {"type": "error", "error": {"type": "api_error", "message": message}}
        return b"event: error\ndata: " + json.dumps(body).encode() + b"\n\n"
    body = {"error": {"message": message, "type": code, "code": code}}
    return b"data: " + json.dumps(body).encode() + b"\n\ndata: [DONE]\n\n"


async def with_heartbeat(
    chunks: AsyncIterator[bytes], *, ping: bytes, interval_s: float
) -> AsyncIterator[bytes]:
    """Repassa `chunks` e injeta `ping` a cada `interval_s` de silêncio.

    O ping só entra numa FRONTEIRA de evento SSE (o último chunk terminou em
    linha em branco): no meio de um evento ele corromperia o JSON que o
    cliente está montando. Chunk que termina no meio de um evento adia o ping
    até a fronteira seguinte — o próximo chunk costuma vir logo.

    Mesmo pump manual do aiter_bytes_watchdog, e pelo mesmo motivo: sem
    asyncio.wait_for, que cancelaria o __anext__ pendente a cada tique."""
    iterator = chunks.__aiter__()
    pending = None
    at_boundary = True
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval_s or None)
            if not done:
                if at_boundary:
                    yield ping
                continue
            finished, pending = pending, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                return
            if chunk:
                yield chunk
                at_boundary = chunk.endswith(b"\n\n") or chunk.endswith(b"\r\n\r\n")
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
