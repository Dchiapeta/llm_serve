"""Política do proxy do agent: allowlist de rotas e isolamento de prefix cache.

Funções PURAS, sem env/rede/FastAPI — importáveis pelos testes sem subir o
agent (mesma disciplina de docker/gateway/context_budget.py).

---------------------------------------------------------------------------
Por que o salt existe
---------------------------------------------------------------------------
Nos planos de pod COMPARTILHADO (Go, Pro) várias stacks de contas
diferentes dividem o mesmo processo vLLM. Com prefix caching ligado, um
cache hit reduz o TTFT de forma observável: um tenant mede o próprio tempo
de resposta e infere se um prefixo já foi processado por outro. Canal
lateral de tempo, e foi por isso que o caching ficou desligado até agora.

O vLLM 0.24.0 resolve com `cache_salt`: o valor entra no hash do PRIMEIRO
bloco de KV e o encadeamento propaga pro resto da sequência
(vllm/v1/core/kv_cache_utils.py). Salts distintos nunca colidem no cache;
o mesmo tenant segue reaproveitando o próprio prefixo entre turnos. Vale
para todos os grupos de KV, inclusive o grupo mamba dos modelos híbridos
(Qwen3.5/3.6) — o hash é da REQUEST, e cada grupo indexa pelo mesmo
BlockHash.

---------------------------------------------------------------------------
INVARIANTE: a granularidade do salt NUNCA pode ser mais grossa que a stack
---------------------------------------------------------------------------
Salt por conta ou por plano reintroduz exatamente o vazamento que isto
fecha. A stack é a fronteira de isolamento que o resto do sistema já usa
(adapter LoRA por stack, system_prompt/RAG por stack) — e é o que permite
dois devs da MESMA stack compartilharem o prefixo de ~26k tokens de
system+tools do Claude Code, que é de onde vem quase todo o ganho.

---------------------------------------------------------------------------
Por que aqui no agent, e não no gateway
---------------------------------------------------------------------------
O pod é alcançável direto pela URL pública do RunPod — é a mesma razão de
ALLOWED_V1 e do filtro de modelos `acct-*` já serem duplicados aqui. Se o
salt fosse injetado só no gateway, um tenant chamaria o pod direto mandando
`cache_salt` arbitrário e colidiria de propósito com o cache da vítima. A
defesa que importa é apply_cache_salt SEMPRE descartar o campo do cliente.
"""

import hashlib
import hmac
import json

# Paths do vLLM que o agent repassa — mesmo conjunto que o gateway permite
# (docker/gateway/main.py ALLOWED_V1). Sem esta allowlist aqui TAMBÉM, quem
# descobrisse a URL pública do pod (proxy da RunPod) podia chamar endpoints
# administrativos do vLLM (load/unload_lora_adapter etc.) direto, contornando
# qualquer allowlist que existisse só no gateway.
ALLOWED_V1: dict[str, set[str]] = {
    "chat/completions": {"POST"},
    "completions": {"POST"},
    "embeddings": {"POST"},
    "models": {"GET"},
    "responses": {"POST"},  # Codex CLI (0.59+) só fala essa API
    # Pod de geração de imagem (docker/image/server.py). O agent é o mesmo
    # binário nos dois tipos de pod, então estas rotas existem na allowlist
    # também nos pods de vLLM — lá elas caem no 404 do próprio vLLM, que é o
    # mesmo status que o cliente já recebia, uma camada acima.
    "images/generations": {"POST"},
    "images/edits": {"POST"},  # multipart: image-to-image
}

# Rotas que aceitam `cache_salt` no corpo (os 3 protocolos generativos do
# vLLM 0.24.0). SALT_EXEMPT_PATHS existe pra que o teste-guarda em
# test_proxy_policy.py force uma decisão CONSCIENTE sobre cada POST novo:
# uma allowlist positiva sozinha falharia aberta (endpoint novo entra em
# ALLOWED_V1, ninguém lembra do salt, e ele passa sem isolamento).
SALTED_PATHS = frozenset({"chat/completions", "completions", "responses"})
# images/*: pipeline de difusão não tem KV cache, então não existe canal lateral
# de prefixo para isolar — o salt não teria nada em que morder. A decisão é
# registrada aqui (e não pela ausência em SALTED_PATHS) porque é o que o
# teste-guarda exige: toda rota POST nova tem que estar de um lado ou do outro.
SALT_EXEMPT_PATHS = frozenset({"embeddings", "images/generations", "images/edits"})

# ---------------------------------------------------------------------------
# Teto de corpo por rota
# ---------------------------------------------------------------------------
#
# O agent não tinha teto NENHUM: `await request.body()` materializava o que
# viesse. Como o pod é alcançável direto pela URL pública do RunPod (é a mesma
# razão da allowlist acima existir aqui e não só no gateway), bastava uma chave
# válida pra mandar um corpo de vários GB e derrubar o pod por RAM — sem passar
# perto do gateway.
#
# Por rota, e não um número global, porque as rotas não têm nada a ver entre si:
# um prompt de chat e um multipart com quatro imagens de referência diferem em
# duas ordens de grandeza, e o teto de um seria absurdo pro outro.
#
# ATENÇÃO ao valor das rotas de LLM: ele NÃO é o MAX_BODY_BYTES de 8 MB do
# gateway, e a folga é deliberada. O gateway corta o corpo do CLIENTE e só
# depois injeta model, system prompt da stack, contexto do RAG e stream_options
# (validate_body em docker/gateway/main.py) — um corpo aceito perto do teto de
# lá chega AQUI maior do que entrou. Espelhar os 8 MB faria o agent recusar
# requisição que o gateway já aprovou, e o cliente veria um 413 sem ter como
# saber o que reduzir.
_MIB = 1024 * 1024
MAX_BODY_BYTES_BY_PATH: dict[str, int] = {
    "chat/completions": 16 * _MIB,
    "completions": 16 * _MIB,
    "responses": 16 * _MIB,
    "embeddings": 16 * _MIB,
    "models": 16 * _MIB,  # GET; o teto existe pra não haver rota sem teto
    # texto puro (prompt + escalares) — três ordens de grandeza abaixo
    "images/generations": 256 * 1024,
    # IMAGE_MAX_REFERENCE_IMAGES × IMAGE_MAX_FILE_SIZE_MB (4 × 15 MiB no
    # template atual) + folga pras boundaries e campos de texto do multipart.
    # Espelha max_edit_bytes() de docker/gateway/image_proxy.py: um teto MENOR
    # aqui recusaria no pod o que o gateway deixou passar.
    "images/edits": 4 * 15 * _MIB + _MIB,
}

# Teto de quem não está no mapa. Só é alcançável se alguém adicionar rota à
# ALLOWED_V1 sem entrada aqui — o que o teste-guarda de test_proxy_policy.py
# impede. Fica como o conservador, e não como o maior dos valores: falhar
# apertado é recuperável (413 num caso não previsto), falhar largo é o buraco
# de memória que este mapa existe pra fechar.
DEFAULT_MAX_BODY_BYTES = 256 * 1024


def max_body_bytes(path: str) -> int:
    """Teto de corpo da rota, em bytes."""
    return MAX_BODY_BYTES_BY_PATH.get(path, DEFAULT_MAX_BODY_BYTES)


class BodyTooLarge(ValueError):
    """Corpo passou do teto da rota. O chamador traduz para 413.

    Levantada DURANTE a leitura (ver read_body_capped), não depois: o ponto é
    justamente nunca ter o corpo inteiro em memória pra poder medi-lo.
    """

    def __init__(self, path: str, ceiling: int):
        self.path = path
        self.ceiling = ceiling
        super().__init__(f"corpo de /v1/{path} excede {ceiling} bytes")


def declared_length_exceeds(content_length: str | None, ceiling: int) -> bool:
    """O Content-Length declarado já estoura o teto?

    Corte barato e antecipado: um cliente honesto declara o tamanho, e aí o 413
    sai sem ler byte nenhum. Header ausente (chunked) ou ilegível devolve False
    — quem cobre esse caso é a contagem incremental, não este atalho. Confiar
    no header pra ACEITAR seria o erro; ele só é usado pra RECUSAR cedo.
    """
    if not content_length or not content_length.isdigit():
        return False
    return int(content_length) > ceiling


async def read_body_capped(chunks, path: str, ceiling: int) -> bytes:
    """Acumula o corpo cortando no teto, em vez de `await request.body()`.

    O agent PRECISA do corpo inteiro (prepare_proxy_body faz json.loads nele), e
    isso não muda — o que muda é o pior caso. Com `await request.body()` seguido
    de um `len(body) > ceiling`, um corpo de 10 GB é integralmente carregado
    antes da comparação: a checagem acontece quando o dano já foi feito. Aqui a
    memória nunca passa do teto, porque a exceção sobe no chunk que o cruza.

    É a metade que o `declared_length_exceeds` não cobre: sem Content-Length
    (Transfer-Encoding: chunked) não existe header pra consultar, e esse era
    exatamente o caminho sem proteção nenhuma.
    """
    parts: list[bytes] = []
    seen = 0
    async for chunk in chunks:
        seen += len(chunk)
        if seen > ceiling:
            # solta o que já foi lido antes de propagar: o handler ainda vai
            # montar a resposta de erro, e segurar centenas de MiB até o
            # unwinding terminar desfaria metade do ganho
            parts.clear()
            raise BodyTooLarge(path, ceiling)
        parts.append(chunk)
    return b"".join(parts)

# Prefixo versionado no HMAC: se um dia a granularidade ou o formato do
# identificador mudar, bumpar isto invalida os salts antigos de uma vez em
# vez de deixar dois esquemas coexistindo no mesmo pool de cache.
_SALT_DOMAIN = "cache-salt:v1:"

# memo do HMAC — o salt é recalculado a cada request e o ident muda pouco
_salt_cache: dict[tuple[str, str], str] = {}


def salt_ident(entry: dict) -> str:
    """Identificador de isolamento de uma entrada de chave.

    EXATAMENTE duas ramificações, nunca uma cadeia de três: qualquer fallback
    a mais é mais uma chance de o mesmo tenant receber idents diferentes em
    momentos diferentes, e cada troca de ident invalida 100% do cache dele.

        stack_id          quando presente e truthy
        "kh:" + key_hash  caso contrário

    O ramo `kh:` é degradado de propósito: isola igual (chave é por conta),
    mas fragmenta o cache entre chaves da mesma stack e não sobrevive a uma
    rotação de chave. Só deve aparecer com agent novo + gateway/painel antigo
    (ver merge_key_entry)."""
    stack_id = entry.get("stack_id")
    if stack_id:
        return str(stack_id)
    return "kh:" + str(entry.get("key_hash") or "")


def tenant_cache_salt(ident: str, secret: str) -> str:
    """HMAC-SHA256(segredo do pod, domínio + ident).

    HMAC e não o ident cru por dois motivos: o AGENT_ADMIN_SECRET é por pod e
    nunca sai dele, e um erro de validação do pydantic no vLLM pode ecoar o
    corpo da request — com o ident cru isso vazaria o stack_id de volta pro
    cliente."""
    cached = _salt_cache.get((ident, secret))
    if cached is not None:
        return cached
    salt = hmac.new(
        secret.encode(), (_SALT_DOMAIN + ident).encode(), hashlib.sha256
    ).hexdigest()
    _salt_cache[(ident, secret)] = salt
    return salt


def merge_key_entry(prev: dict | None, incoming: dict, key_hash: str) -> dict:
    """Monta a entrada de keys_by_hash preservando o stack_id já conhecido.

    O agent SUBSTITUI a entrada inteira a cada /admin/sync-keys e
    /admin/upsert-keys, e os produtores de payload divergem: nem todos
    mandam stack_id (versões antigas do gateway/painel, ou um deploy parcial).
    Sem carry-over, o salt alternaria entre `stack_id` e `kh:*` a cada
    re-sync e **invalidaria todo o cache do tenant a cada flip** — o pior dos
    dois mundos, porque o custo de VRAM do caching continuaria sendo pago.

    Regra: `stack_id` AUSENTE no payload preserva o valor conhecido;
    PRESENTE (mesmo None) sobrescreve — é como uma stack desvinculada
    consegue voltar pro ramo `kh:`.

    O carry-over vale SÓ pra stack_id. Em expires_at seria um bug de
    segurança: um None novo precisa poder limpar uma expiração antiga."""
    prev = prev or {}
    entry = {
        "api_key_id": incoming.get("api_key_id"),
        "key_prefix": incoming.get("key_prefix", "?"),
        "account_name": incoming.get("account_name", "?"),
        "expires_at": incoming.get("expires_at"),
        "key_hash": key_hash,
    }
    entry["stack_id"] = (
        incoming.get("stack_id") if "stack_id" in incoming else prev.get("stack_id")
    )
    return entry


def apply_cache_salt(
    body_json: dict, path: str, salt: str | None, enabled: bool
) -> dict:
    """Descarta o `cache_salt` do cliente e injeta o do tenant quando cabe.

    O pop é INCONDICIONAL — é a defesa contra quem chama o pod direto pela
    URL pública tentando colidir de propósito com o cache de outro tenant.
    Vale inclusive com o salting desligado: aí o campo simplesmente não
    existe pra ninguém, em vez de existir só pra quem souber mandá-lo."""
    body_json.pop("cache_salt", None)
    if enabled and salt and path in SALTED_PATHS:
        body_json["cache_salt"] = salt
    return body_json


class UnparseableBody(ValueError):
    """Corpo que precisa de salt e não é um objeto JSON. O chamador traduz
    para 400 — deixar passar seria uma request sem salt num pod COM caching."""


# ---------------------------------------------------------------------------
# Content-Type de upstream
# ---------------------------------------------------------------------------


def upstream_content_type_for(client_content_type: str) -> str:
    """Content-Type a enviar ao servidor de inferência.

    Duas regras, e as duas têm motivo concreto:

    1. Fora do multipart o header é FORÇADO em "application/json", não
       repassado. Cliente que manda corpo JSON declarando "text/plain"
       (acontece em HTTP client simples) funciona hoje justamente porque o
       agent corrige o header aqui — repassar cru passaria a dar 422.

    2. No multipart o header é repassado, porque é nele que viaja o
       `boundary=...` que delimita as partes — sobrescrevê-lo torna o corpo
       impossível de parsear. Mas o MEDIA TYPE é normalizado para minúsculas,
       preservando os parâmetros verbatim.

    O porquê da normalização, que não é teórico: media type é case-insensitive
    (RFC 9110 §8.3.1), mas o `request.form()` do Starlette compara o valor
    parseado com o literal b"multipart/form-data" SEM normalizar. Um cliente que
    mande "Multipart/Form-Data" chega com o header intacto e ainda assim cai no
    ramo de form VAZIO — a rota responde "missing_image" em vez de processar as
    imagens. Medido no container.

    O `boundary` NÃO pode ser normalizado: é case-sensitive, e minusculizá-lo
    quebraria a delimitação das partes.
    """
    media_type, sep, params = client_content_type.partition(";")
    if not media_type.strip().lower().startswith("multipart/"):
        return "application/json"
    return media_type.strip().lower() + sep + params


def prepare_proxy_body(
    raw: bytes, path: str, entry: dict, *, salt_enabled: bool, secret: str
) -> tuple[bytes, bool]:
    """Reescreve o corpo antes do proxy. Devolve (corpo, is_stream).

    Faz três coisas de uma vez porque as três dependem de parsear o JSON uma
    única vez: detecta streaming, injeta stream_options.include_usage (sem
    isso o vLLM não manda o chunk final com usage e nenhum token é contado) e
    aplica a política de cache_salt.

    Levanta UnparseableBody quando o corpo não é um objeto JSON num path que
    exige salt. Nos outros paths um corpo ilegível segue adiante como antes —
    quem valida o formato é o vLLM."""
    try:
        body_json = json.loads(raw)
    except Exception:
        body_json = None

    if not isinstance(body_json, dict):
        if salt_enabled and path in SALTED_PATHS:
            raise UnparseableBody(path)
        return raw, False

    is_stream = body_json.get("stream") is True
    if is_stream:
        # get + isinstance em vez de setdefault: um "stream_options": null
        # explícito do cliente faria o setdefault devolver None e o
        # .setdefault seguinte estourar AttributeError. Antes isso era
        # engolido por um `except Exception: pass` no chamador; agora que a
        # reescrita do corpo é obrigatória, não pode mais escapar.
        opts = body_json.get("stream_options")
        if not isinstance(opts, dict):
            opts = {}
            body_json["stream_options"] = opts
        opts.setdefault("include_usage", True)

    apply_cache_salt(
        body_json,
        path,
        tenant_cache_salt(salt_ident(entry), secret) if secret else None,
        salt_enabled,
    )
    return json.dumps(body_json).encode(), is_stream
