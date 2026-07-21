"""
Gateway estável de inferência — o ÚNICO endpoint público que o cliente final
conhece. Resolve em qual máquina está o adapter LoRA da conta e faz o proxy
(incluindo streaming SSE) para o agent daquele pod. O cliente nunca sabe em
qual pod está.

Fluxo por request:
  1. Autentica a chave HEX (Bearer) contra api_keys no Supabase (cache TTL),
     junto com o plano e o system_prompt configurados da stack da chave.
  2. Resolve a rota (routing_state). Regra primária: machine_id definido →
     proxy direto, independente do status (em 'migrating' a origem segue
     servindo até o flip). Só espera quando não há máquina servindo. Sem
     adapter, o modelo base é stack-aware: serve na máquina do stack da
     conta; pausada → realocação automática (reponta stack + move chaves)
     pra outra running com vaga do MESMO plano, ou religa a própria; sem
     stack, fallback por plano — nunca cai no modelo base de outro plano.
  3. Sem rota: alocação placeholder (primeira máquina running com slot livre),
     claim atômico, upsert da chave no agent, load do adapter, proxy.
  4. Injeta no body (chat completions): system prompt da conta + top-k de
     contexto da base de conhecimento (RAG básico do VibeCoder, embeddings
     via OpenAI).
  5. Máquina fora do ar → 503 imediato, nunca pendura o request.
  6. Sem nenhuma máquina running com vaga → auto-wake: religa (startPod) um
     pod pausado do template do plano e responde 503 + Retry-After; o retry
     do cliente aloca normalmente quando o vLLM estiver de pé. Toda religada
     agenda o reenvio das chaves ao agent (que reinicia zerado) e o fluxo
     base ainda garante a chave via upsert lazy antes de cada proxy.

Limitação aceita (MVP): réplica ÚNICA. O contador in-flight e (na Fase 5) o
idle reaper vivem em memória do processo — múltiplas réplicas cortariam
streams durante migração. Ver README.md.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from anthropic_compat import (
    anthropic_sse_from_openai_stream,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from context_budget import (
    ContextWindowExceeded,
    anthropic_error_body,
    apply_context_budget,
    estimate_prompt_tokens,
    openai_error_body,
    prompt_text_for_tokenize,
    should_use_exact_token_count,
)
from lifecycle import LifecycleManager, MigrationError
from usage_class import class_weight, classify_stack
from routing import RoutingStore
from runpod_api import RunPodClient
from supa import SupaClient

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GATEWAY_ADMIN_SECRET = os.environ.get("GATEWAY_ADMIN_SECRET", "")
KEY_CACHE_TTL_S = float(os.environ.get("KEY_CACHE_TTL_S", "60"))
KEY_CACHE_NEGATIVE_TTL_S = 5.0
LORA_LOAD_TIMEOUT_S = float(os.environ.get("LORA_LOAD_TIMEOUT_S", "120"))
LORA_BUCKET = os.environ.get("LORA_BUCKET", "loras")
# embeddings do RAG (VibeCoder) — mesmo modelo/dimensão usado na indexação
# pelo painel (lib/actions.ts), senão a similaridade de cosseno não faz sentido
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))
# teto de adapters por máquina usado na alocação placeholder; a Fase 6 troca
# isso pelo cálculo de capacidade por VRAM (machine_lora_slots)
MAX_LORAS_PER_MACHINE = int(os.environ.get("MAX_LORAS_PER_MACHINE", "8"))

# ---------- limites de abuso/custo por chave (produção multi-tenant) ----------
# rate limit (token bucket, req/min) por chave — sem isso uma chave vazada ou
# um cliente descontrolado consumia GPU sem nenhum teto. Réplica única do
# gateway (ver docstring do módulo), então estado em memória é seguro — mesmo
# padrão do key_cache/in_flight.
RATE_LIMIT_RPM = float(os.environ.get("RATE_LIMIT_RPM", "60"))

# concorrência: ELÁSTICA por MÁQUINA, não um teto fixo por chave — uma stack
# sozinha no pod pode usar quase toda a capacidade; outras dividem o mesmo
# teto conforme aparecem (ver check_concurrency). DEFAULT_MAX_CONCURRENT_SEQS
# só vale quando machines.max_concurrent_seqs (migration 0028) não foi
# preenchido pro pod ainda — é o fallback conservador, não a capacidade real.
DEFAULT_MAX_CONCURRENT_SEQS = int(os.environ.get("DEFAULT_MAX_CONCURRENT_SEQS", "8"))
# pod compartilhado (SHARED_POD_PLANS) sempre reserva esse mínimo de vagas —
# garante que quem chegar depois de um tenant pesado nunca fica 100%
# bloqueado esperando, só entra numa fila menor. Pod dedicado não reserva
# nada: não há vizinho pra proteger.
MIN_RESERVED_SLOTS_SHARED_POD = int(os.environ.get("MIN_RESERVED_SLOTS_SHARED_POD", "2"))
# corpo/params da request — nenhum destes existia antes: sem teto, um
# cliente BYOE podia mandar prompt gigante sem limite de tamanho/mensagens
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(1_000_000)))
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "200"))

# quota diária de tokens por conta (controle de custo real — rate limit e
# concorrência limitam volume de requests, não o custo de cada uma). 0 =
# sem teto (default, por plano — só liga onde configurado). Lida de
# usage_metrics, populada pelo metrics_collection_loop abaixo; cache curto
# evita 1 round-trip ao Supabase por request na hot path.
DAILY_TOKEN_BUDGET = {
    "VibeCoder": int(os.environ.get("DAILY_TOKEN_BUDGET_VIBECODER", "0")),
    "Pro": int(os.environ.get("DAILY_TOKEN_BUDGET_PRO", "0")),
    "Max": int(os.environ.get("DAILY_TOKEN_BUDGET_MAX", "0")),
    "Enterprise": int(os.environ.get("DAILY_TOKEN_BUDGET_ENTERPRISE", "0")),
}
TOKEN_QUOTA_CACHE_TTL_S = float(os.environ.get("TOKEN_QUOTA_CACHE_TTL_S", "60"))
# usage_metrics antes só era populado quando um admin abria o painel
# (collectUsageMetrics em lib/metrics.ts, chamado só no carregamento da
# página) — inviável como base de uma quota real, já que uma conta gerava
# uso ilimitado entre duas visitas ao painel sem nenhum registro. Este loop
# espelha aquela coleta, mas roda sozinho no processo do gateway.
METRICS_COLLECTION_INTERVAL_S = float(os.environ.get("METRICS_COLLECTION_INTERVAL_S", "120"))

# classificação de stacks por padrão de consumo (usage_class, migration 0032):
# janela móvel de avaliação, mínimo de dias ativos pra classificar, cooldown
# entre mudanças (histerese — um dia atípico não muda classe) e cadência do
# loop. A classe só influencia alocações FUTURAS; 6h de cadência é mais que
# suficiente pra um sinal que exige dias de uso sustentado.
USAGE_CLASS_WINDOW_DAYS = int(os.environ.get("USAGE_CLASS_WINDOW_DAYS", "14"))
USAGE_CLASS_MIN_ACTIVE_DAYS = int(os.environ.get("USAGE_CLASS_MIN_ACTIVE_DAYS", "5"))
USAGE_CLASS_COOLDOWN_DAYS = int(os.environ.get("USAGE_CLASS_COOLDOWN_DAYS", "7"))
USAGE_CLASS_INTERVAL_S = float(os.environ.get("USAGE_CLASS_INTERVAL_S", "21600"))

# quanto tempo um request espera por um load em andamento de outro request
LOAD_WAIT_TIMEOUT_S = float(os.environ.get("LOAD_WAIT_TIMEOUT_S", "20"))
TOUCH_THROTTLE_S = 15.0
# lifecycle: liberação do slot por ociosidade (0 = desligado) e drain da
# migração. IDLE_RELEASE_MINUTES substitui IDLE_UNLOAD_MINUTES (fallback
# mantido pra não quebrar deploy existente).
IDLE_RELEASE_MINUTES = float(
    os.environ.get("IDLE_RELEASE_MINUTES", os.environ.get("IDLE_UNLOAD_MINUTES", "30"))
)
MIGRATION_DRAIN_TIMEOUT_S = float(os.environ.get("MIGRATION_DRAIN_TIMEOUT_S", "600"))
# staleness de routing_state presa em loading/migrating (ver
# reconcile_stale_routes_once) — bem acima de LORA_LOAD_TIMEOUT_S/
# MIGRATION_DRAIN_TIMEOUT_S de propósito, pra nunca competir com uma
# operação genuinamente em andamento e só pegar o que ficou preso de fato
STALE_ROUTE_THRESHOLD_S = float(os.environ.get("STALE_ROUTE_THRESHOLD_S", "1800"))
STALE_ROUTE_CHECK_INTERVAL_S = float(os.environ.get("STALE_ROUTE_CHECK_INTERVAL_S", "300"))
# lifecycle de máquinas: consolidação (esvaziar máquina quase vazia migrando
# as contas pra outra do mesmo template), auto-pausa (stopPod) de máquina sem
# nenhuma atividade e auto-wake (startPod) quando chega request sem nenhuma
# máquina running com vaga. Ambos exigem RUNPOD_API_KEY.
MACHINE_IDLE_STOP_MINUTES = float(os.environ.get("MACHINE_IDLE_STOP_MINUTES", "30"))
WAKE_COOLDOWN_S = float(os.environ.get("WAKE_COOLDOWN_S", "120"))
CONSOLIDATION_INTERVAL_S = float(os.environ.get("CONSOLIDATION_INTERVAL_S", "300"))
CONSOLIDATION_MAX_ORIGIN_ROUTES = int(os.environ.get("CONSOLIDATION_MAX_ORIGIN_ROUTES", "2"))
STOP_RECHECK_GRACE_S = float(os.environ.get("STOP_RECHECK_GRACE_S", "5"))
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")

# provisionamento automático de máquina (3º nível da cascata de alocação,
# além de rodando-com-vaga e despausar): o gateway nunca fala com a API de
# criação da RunPod diretamente — chama de volta o painel Next.js (que já
# tem toda a lógica de GPU/template/stockout), via PANEL_URL protegido por
# um secret dedicado (não reaproveita GATEWAY_ADMIN_SECRET — ver README).
# Sem PANEL_URL/PANEL_ADMIN_SECRET, esse nível fica desligado (mesmo padrão
# de RUNPOD_API_KEY ausente).
PANEL_URL = os.environ.get("PANEL_URL", "").rstrip("/")
PANEL_ADMIN_SECRET = os.environ.get("PANEL_ADMIN_SECRET", "")
PANEL_PROVISION_TIMEOUT_S = float(os.environ.get("PANEL_PROVISION_TIMEOUT_S", "60"))
# intervalo mínimo entre tentativas de criação por plano — evita bombardear
# o painel/RunPod com requests concorrentes ou stockout repetido
PROVISION_COOLDOWN_S = float(os.environ.get("PROVISION_COOLDOWN_S", "180"))
# criar+subir é mais lento que só religar (pode incluir pull de imagem e
# download de pesos do zero num host novo) — Retry-After maior que o do wake
PROVISION_RETRY_AFTER_S = float(os.environ.get("PROVISION_RETRY_AFTER_S", "120"))
# recriação (delete + create + start num host novo): destrutiva e cara, então
# cooldown por máquina mais folgado que o do wake. Retry-After alinhado ao do
# provisionamento (é o mesmo custo de subir um pod do zero).
RECREATE_COOLDOWN_S = float(os.environ.get("RECREATE_COOLDOWN_S", "300"))
RECREATE_RETRY_AFTER_S = float(os.environ.get("RECREATE_RETRY_AFTER_S", "120"))
# soma mínima de slots livres do plano (running + a capacidade cheia de uma
# reserva pausada) — abaixo disso, o loop proativo cria+pausa uma máquina nova
MACHINE_POOL_WATERMARK_SLOTS = float(os.environ.get("MACHINE_POOL_WATERMARK_SLOTS", "5"))
MACHINE_HEALTH_TIMEOUT_S = float(os.environ.get("MACHINE_HEALTH_TIMEOUT_S", "900"))
MACHINE_HEALTH_POLL_INTERVAL_S = float(os.environ.get("MACHINE_HEALTH_POLL_INTERVAL_S", "10"))
# TTL do cache em memória do interruptor liga/desliga (system_settings) —
# evita 1 round-trip ao Supabase por request na hot path
SETTINGS_CACHE_TTL_S = float(os.environ.get("SETTINGS_CACHE_TTL_S", "30"))
# TTL do cache "chave já upsertada no agent X" — o agent perde as chaves em
# memória a cada restart do pod, então o fluxo base garante a chave via
# upsert lazy antes do proxy; o cache evita 1 round-trip ao agent por request
UPSERT_CACHE_TTL_S = float(os.environ.get("UPSERT_CACHE_TTL_S", "600"))

STARTED_AT = time.time()

supa: SupaClient
store: RoutingStore
# proxy para os agents: connect curto (máquina fora do ar → 503 rápido),
# read longo (streams de inferência podem durar minutos)
proxy_client: httpx.AsyncClient
# client curto pra API de embeddings da OpenAI (RAG do VibeCoder)
openai_client: httpx.AsyncClient
# client pra chamar de volta o painel Next.js (POST /api/machines/provision)
panel_client: httpx.AsyncClient

# cache de chaves: key_hash -> (entry | None, expira_em)
key_cache: dict[str, tuple[dict | None, float]] = {}

# cache do interruptor liga/desliga (system_settings.auto_provision_enabled)
auto_provision_cache: tuple[bool, float] | None = None

# requests em voo por (account_id, machine_id) — base do drain da Fase 5.
# Em memória: válido apenas com réplica única do gateway.
in_flight: dict[tuple[str, str], int] = defaultdict(int)

# rate limit (token bucket) por chave — key_hash, não account_id: uma conta
# pode ter várias chaves, cada uma com seu próprio teto de RPM
rate_buckets: dict[str, tuple[float, float]] = {}  # key_hash -> (tokens, last_refill_ts)

# cache curto da quota diária de tokens: account_id -> (tokens_usados, expira_em)
token_usage_cache: dict[str, tuple[int, float]] = {}

# último touch por conta e por máquina (throttle)
last_touch: dict[str, float] = {}
last_machine_touch: dict[str, float] = {}
# último touch de stacks.last_activity_at por stack (throttle) — relógio de
# ociosidade do modelo base, base do reap_idle_base_stacks_once (lifecycle)
last_stack_touch: dict[str, float] = {}

# última tentativa de auto-wake por máquina — evita tempestade de startPod
# com requests concorrentes ou falhas repetidas (ex.: host sem GPU livre)
last_wake_attempt: dict[str, float] = {}

# chaves já garantidas no agent: (key_hash, machine_id) -> expira_em.
# Invalidado por máquina a cada religada (o agent volta sem chaves).
agent_key_upserts: dict[tuple[str, str], float] = {}

# máquinas com re-sync de chaves agendado/em andamento (pós-religada) —
# mesma disciplina do provisioning_in_progress
key_sync_in_progress: set[str] = set()

# serializa a realocação de stacks por plano: escolher alvo + contar vaga +
# repontar precisa ser atômico entre requests concorrentes (réplica única)
realloc_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# provisionamento automático: plano -> criação em andamento (cascata reativa
# e reposição proativa compartilham essa trava, pra nunca criar 2 máquinas
# concorrentes pro mesmo plano) e última tentativa (cooldown)
provisioning_in_progress: set[str] = set()
last_provision_attempt: dict[str, float] = {}

# recriação automática: máquina -> recriação em andamento (trava contra
# requests concorrentes recriarem o mesmo pod) e última tentativa (cooldown).
# Disparada quando o auto-wake falha por "not enough free GPUs" — o host cedeu
# a GPU do pod pausado e só recriar num host novo o traz de volta.
recreating_in_progress: set[str] = set()
last_recreate_attempt: dict[str, float] = {}
# fila de recriações pendentes: máquinas que o caminho reativo (no_gpu) marcou
# pra recriar e ainda não confirmaram sucesso. O lifecycle loop reprocessa
# (process_pending_recreates_once) — mesma disciplina do pending_unloads —
# garantindo o retry se a chamada ao painel falhar/cair, sem depender de um
# request específico. Uma máquina sai da fila quando a recriação conclui (ou
# quando ela deixa de estar stopped/error, ex.: subiu por outro caminho).
pending_recreates: set[str] = set()

logger = logging.getLogger("gateway")

lifecycle_mgr: "LifecycleManager"
runpod_client: RunPodClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global supa, store, proxy_client, openai_client, panel_client, lifecycle_mgr, runpod_client
    supa = SupaClient(SUPABASE_URL, SERVICE_ROLE_KEY, LORA_BUCKET)
    store = RoutingStore(SUPABASE_URL, SERVICE_ROLE_KEY)
    # read curto (60s): o Cloudflare na frente do RunPod às vezes derruba (RST)
    # conexões TCP em keep-alive; sem isso, uma conexão zumbi reaproveitada
    # do pool prendia o cliente por até 600s esperando um socket morto.
    # 60s é folgado pro maior gap real entre chunks de streaming — TTFT e
    # geração ficam bem abaixo disso mesmo sob 20 concorrentes.
    # retries=2: mesmo com keepalive_expiry curto, uma conexão do pool pode
    # ser resetada pelo Cloudflare ENQUANTO ociosa dentro da janela de expiry
    # — a primeira escrita nela falha na hora (ConnectError). O transporte do
    # httpx detecta e reabre uma conexão nova automaticamente antes de
    # qualquer byte ir pro cliente (visto sob concorrência 5-20: sem isso,
    # a requisição inteira falhava em ~3s com corpo vazio, sem erro visível).
    proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=5.0, write=10.0, pool=10.0),
        limits=httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0
        ),
        transport=httpx.AsyncHTTPTransport(retries=2),
    )
    openai_client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=httpx.Timeout(20.0, connect=5.0),
    )
    panel_client = httpx.AsyncClient(
        timeout=httpx.Timeout(PANEL_PROVISION_TIMEOUT_S, connect=5.0)
    )
    if RUNPOD_API_KEY:
        runpod_client = RunPodClient(RUNPOD_API_KEY)
    else:
        runpod_client = None
        logger.warning(
            "RUNPOD_API_KEY ausente — auto-pausa e auto-wake de máquinas desligados"
        )
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        logger.warning(
            "PANEL_URL/PANEL_ADMIN_SECRET ausente — provisionamento automático de máquina desligado"
        )
    lifecycle_mgr = LifecycleManager(
        store=store,
        supa=supa,
        call_agent=call_agent,
        in_flight=in_flight,
        idle_unload_minutes=IDLE_RELEASE_MINUTES,
        drain_timeout_s=MIGRATION_DRAIN_TIMEOUT_S,
        lora_load_timeout_s=LORA_LOAD_TIMEOUT_S,
        machine_free_slots=machine_free_slots,
        runpod=runpod_client,
        machine_idle_stop_minutes=MACHINE_IDLE_STOP_MINUTES,
        consolidation_max_origin_routes=CONSOLIDATION_MAX_ORIGIN_ROUTES,
        stop_recheck_grace_s=STOP_RECHECK_GRACE_S,
        try_provision_for_pool=try_provision_for_pool,
        pool_watermark_slots=MACHINE_POOL_WATERMARK_SLOTS,
        auto_provision_enabled=auto_provision_enabled,
        on_machine_running=handle_machine_running,
        try_recreate_machine=try_recreate_machine,
        pending_recreates=pending_recreates,
    )
    reaper_task = asyncio.create_task(lifecycle_mgr.idle_reaper_loop())
    machine_task = asyncio.create_task(
        lifecycle_mgr.machine_lifecycle_loop(CONSOLIDATION_INTERVAL_S)
    )
    metrics_task = asyncio.create_task(metrics_collection_loop())
    stale_routes_task = asyncio.create_task(stale_route_reconciliation_loop())
    usage_class_task = asyncio.create_task(usage_class_loop())
    yield
    reaper_task.cancel()
    machine_task.cancel()
    metrics_task.cancel()
    stale_routes_task.cancel()
    usage_class_task.cancel()
    await proxy_client.aclose()
    await openai_client.aclose()
    await panel_client.aclose()
    await store.aclose()
    await supa.aclose()
    if runpod_client:
        await runpod_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(ContextWindowExceeded)
async def context_window_exceeded_handler(request: Request, exc: ContextWindowExceeded):
    """Formata o estouro de contexto no shape do cliente. Handler dedicado à
    SUBCLASSE (o Starlette resolve por MRO, então vence o default de
    HTTPException) — os demais HTTPException seguem no {"detail": ...} padrão,
    que painel/scripts já parseiam."""
    if request.url.path.startswith("/v1/messages"):
        return JSONResponse(status_code=exc.status_code, content=anthropic_error_body(exc.detail))
    return JSONResponse(status_code=exc.status_code, content=openai_error_body(exc.detail))


# O gateway é uma API pra clientes programáticos (SDK OpenAI/Anthropic,
# Codex/Claude Code, BYOE) — nunca chamada de um browser. CORS explícito e
# fechado em vez de ausente (o padrão do Starlette sem CORSMiddleware
# nenhum já bloqueia por padrão, mas fica implícito; aqui fica documentado
# e fácil de abrir uma origem específica no futuro, se algum dia existir
# um playground no browser).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "x-api-key", "Content-Type", "anthropic-version"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # não mexe em headers de cache/transformação: quebraria as respostas
    # SSE (text/event-stream) do proxy de streaming
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # MutableHeaders do Starlette não tem .pop() (só __delitem__) — usar
    # .pop() aqui derrubava TODA requisição do gateway com 500
    if "Server" in response.headers:
        del response.headers["Server"]
    return response


def require_admin(secret: str | None):
    if not GATEWAY_ADMIN_SECRET or not secret or not hmac.compare_digest(secret, GATEWAY_ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="admin secret inválido")


def lora_name(stack_id: str) -> str:
    # Nome do adapter LoRA no vLLM, escopado por STACK (migration 0029). O
    # prefixo "acct-" é mantido por compatibilidade: os filtros de /v1/models
    # (gateway e agent) removem qualquer id que comece com "acct-", então trocar
    # o prefixo exigiria atualizar os dois. Duas stacks da mesma conta agora
    # recebem nomes distintos (acct-<stackA> ≠ acct-<stackB>) — fim da colisão.
    return f"acct-{stack_id}"


# ---------- Autenticação ----------


async def authenticate(authorization: str | None) -> tuple[dict, str]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="chave de acesso ausente")
    key = authorization.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    cached = key_cache.get(key_hash)
    if cached and cached[1] > time.time():
        entry = cached[0]
    else:
        entry = await supa.find_active_key(key_hash)
        ttl = KEY_CACHE_TTL_S if entry else KEY_CACHE_NEGATIVE_TTL_S
        key_cache[key_hash] = (entry, time.time() + ttl)

    if not entry:
        raise HTTPException(status_code=401, detail="chave de acesso inválida")

    # checado por request, não só num filtro na query: o key_cache (TTL de
    # KEY_CACHE_TTL_S) manteria uma chave expirada válida por até mais um
    # TTL depois do vencimento se a expiração dependesse só do PostgREST
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expiry = None
        if expiry and expiry <= datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="chave expirada")

    # fail-closed: toda chave precisa de stack_id resolvido. Plano e
    # system_prompt são propriedade da stack (migration 0027 removeu
    # accounts.plan/system_prompt) — sem stack não há config nenhuma pra
    # resolver, então não existe mais um "roteamento por plano puro" de
    # fallback. A migration 0021 já fez o backfill de stack_id em toda
    # chave ativa de conta com stack; esta checagem é o defense-in-depth
    # pra qualquer chave que escape disso no futuro.
    if not entry.get("stack_id"):
        raise HTTPException(status_code=401, detail="chave sem stack associada — contate o suporte")

    return entry, key_hash


def check_rate_limit(key_hash: str) -> None:
    """Token bucket em memória por chave: RATE_LIMIT_RPM tokens/min, com
    burst até o teto do bucket. Estourou -> 429 + Retry-After; nunca
    enfileira, só rejeita — o cliente decide se tenta de novo."""
    now = time.time()
    tokens, last = rate_buckets.get(key_hash, (RATE_LIMIT_RPM, now))
    tokens = min(RATE_LIMIT_RPM, tokens + (now - last) * RATE_LIMIT_RPM / 60.0)
    if tokens < 1.0:
        rate_buckets[key_hash] = (tokens, now)
        retry_after = max(1, int((1.0 - tokens) * 60.0 / RATE_LIMIT_RPM) + 1)
        raise HTTPException(
            status_code=429,
            detail="limite de requisições excedido, tente novamente em instantes",
            headers={"Retry-After": str(retry_after)},
        )
    rate_buckets[key_hash] = (tokens - 1.0, now)


async def check_token_quota(account_id: str, plan: str | None) -> None:
    """Quota diária de tokens por conta — protege contra custo real (poucas
    requests, cada uma gerando muito token), o que rate limit/concorrência
    por si não cobrem. Lida de usage_metrics via account_token_usage_today,
    com cache curto (TOKEN_QUOTA_CACHE_TTL_S) pra não bater no Supabase a
    cada request. 0/plano sem entrada = sem teto (opt-in por plano); `plan`
    None (chave sem stack resolvível) cai no mesmo caso — resolve_route
    rejeita a request logo em seguida com 503, então não enforça nada aqui
    à toa."""
    budget = DAILY_TOKEN_BUDGET.get(plan, 0)
    if budget <= 0:
        return
    now = time.time()
    cached = token_usage_cache.get(account_id)
    if cached and cached[1] > now:
        used = cached[0]
    else:
        used = await supa.account_token_usage_today(account_id)
        token_usage_cache[account_id] = (used, now + TOKEN_QUOTA_CACHE_TTL_S)
    if used >= budget:
        raise HTTPException(
            status_code=429,
            detail=f"quota diária de tokens excedida ({used}/{budget})",
            headers={"Retry-After": "3600"},
        )


async def authenticate_anthropic(
    authorization: str | None, x_api_key: str | None
) -> tuple[dict, str, str]:
    """Igual a authenticate, mas aceita a chave em Authorization: Bearer OU
    x-api-key — o Claude Code manda num dos dois (às vezes os dois, se o
    usuário configurou apiKeyHelper) dependendo de ANTHROPIC_AUTH_TOKEN vs
    ANTHROPIC_API_KEY. Devolve também o header já normalizado pra "Bearer
    <key>", pra repassar ao agent no upstream (que só entende Bearer, nunca
    x-api-key)."""
    bearer = None
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization.removeprefix("Bearer ").strip()
    key = bearer or (x_api_key.strip() if x_api_key else None)
    if not key:
        raise HTTPException(status_code=401, detail="chave de acesso ausente")
    bearer_header = f"Bearer {key}"
    entry, key_hash = await authenticate(bearer_header)
    return entry, key_hash, bearer_header


# ---------- Roteamento / alocação ----------


async def call_agent(machine: dict, path: str, body: dict, timeout_s: float = 30.0) -> dict:
    """POST /admin/* no agent do pod, autenticado pelo admin_secret da máquina."""
    try:
        r = await proxy_client.post(
            f"{machine['public_url']}/admin{path}",
            json=body,
            headers={"X-Admin-Secret": machine["admin_secret"]},
            timeout=httpx.Timeout(timeout_s, connect=5.0),
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"máquina indisponível: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"agent {path} → {r.status_code}: {r.text}")
    return r.json()


async def call_vllm_tokenize(machine: dict, model: str, text: str) -> int | None:
    """Contagem real de tokens via /admin/tokenize do agent (que chama o
    /tokenize do vLLM local). None em qualquer falha — o chamador cai de
    volta pra estimativa heurística; esta checagem extra nunca pode travar
    uma request por conta própria (pod fora do ar, agent antigo sem o
    endpoint, timeout — tudo cai no mesmo None)."""
    try:
        resp = await call_agent(machine, "/tokenize", {"text": text, "model": model}, timeout_s=8.0)
    except HTTPException:
        return None
    count = resp.get("count")
    return count if isinstance(count, int) else None


async def resolve_est_tokens(
    machine: dict, heuristic_est: int, exact_text: str, tokenize_fn=call_vllm_tokenize
) -> int:
    """Decide o est_tokens final pro apply_context_budget: heurística por
    padrão (fast-path — maioria das requests fica longe do teto), contagem
    real do tokenizer via /tokenize do vLLM só quando should_use_exact_token_count
    já indica proximidade do limite. tokenize_fn injetável só pra testar sem
    precisar mockar httpx."""
    if not should_use_exact_token_count(heuristic_est, machine):
        return heuristic_est
    model = machine.get("served_model_name") or machine.get("model_name")
    if not model:
        return heuristic_est
    exact = await tokenize_fn(machine, model, exact_text)
    return exact if exact is not None else heuristic_est


async def get_agent_metrics(machine: dict, reset: bool = True) -> dict | None:
    """GET /admin/metrics no agent (call_agent só faz POST) — usado só pela
    coleta periódica de uso abaixo, nunca no caminho de request de cliente.
    None em qualquer falha: agent fora do ar não deve derrubar o loop, os
    contadores seguem acumulando no pod até a próxima coleta."""
    try:
        r = await proxy_client.get(
            f"{machine['public_url']}/admin/metrics",
            params={"reset": "true"} if reset else {},
            headers={"X-Admin-Secret": machine["admin_secret"]},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def collect_usage_metrics_once() -> None:
    """Único escritor de usage_metrics: puxa os contadores acumulados do
    agent de cada máquina running (zerando-os na leitura) e grava o delta
    em usage_metrics. Roda sozinha no processo do gateway — antes de existir,
    usage_metrics só era populado quando um admin abria o painel, o que
    deixava a quota diária de tokens (check_token_quota) cega a qualquer
    uso entre duas visitas (esse coletor do painel foi removido depois que
    causou atribuição incorreta a api_key_id=NULL por divergir do esquema
    de chave do agent).

    `per_key` do agent é indexado por api_key_id (não mais por key_prefix):
    o prefixo tem só 32 bits, colisão entre duas contas diferentes não é
    impossível, e usar o prefixo como chave de agregação atribuiria o uso
    de uma conta à outra na eventualidade de colisão — sensível o bastante
    (alimenta a quota diária de custo) pra merecer o identificador estável."""
    machines = await supa.list_running_machines()
    for machine in machines:
        if not machine.get("public_url"):
            continue
        snap = await get_agent_metrics(machine)
        if not snap:
            continue
        active = {
            api_key_id: v for api_key_id, v in (snap.get("per_key") or {}).items()
            if v.get("requests", 0) > 0
        }
        if not active:
            continue
        window_start = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "api_key_id": api_key_id,
                "machine_id": machine["id"],
                "window_start": window_start,
                "requests": v.get("requests", 0),
                "tokens_in": v.get("tokens_in", 0),
                "tokens_out": v.get("tokens_out", 0),
                "concurrent_peak": snap.get("concurrent_peak", 0),
            }
            for api_key_id, v in active.items()
        ]
        try:
            await supa.insert_usage_metrics(rows)
        except Exception as e:
            logger.warning("coleta de métricas: falha ao gravar máquina %s (%s)", machine["id"], e)


async def metrics_collection_loop(interval_s: float = METRICS_COLLECTION_INTERVAL_S):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await collect_usage_metrics_once()
        except Exception as e:
            logger.warning("coleta periódica de métricas falhou: %s", e)


async def classify_stacks_once() -> None:
    """Reclassifica usage_class das stacks pelo consumo real (usage_metrics
    agregado pela RPC stack_usage_stats, migration 0032). A decisão em si é
    pura (usage_class.classify_stack: dois fatores, histerese, cooldown);
    aqui é só o I/O. A classe nova NÃO dispara migração — só pesa nas
    alocações futuras (pick_running_machine_with_stack_slot ponderado)."""
    rows = await supa.stack_usage_stats(USAGE_CLASS_WINDOW_DAYS)
    now = datetime.now(timezone.utc)
    for row in rows:
        new_class = classify_stack(
            row,
            now,
            min_active_days=USAGE_CLASS_MIN_ACTIVE_DAYS,
            cooldown_days=USAGE_CLASS_COOLDOWN_DAYS,
        )
        if not new_class:
            continue
        try:
            await supa.update_stack_usage_class(row["stack_id"], new_class)
            logger.info(
                "usage_class: stack %s reclassificada %s -> %s",
                row["stack_id"], row.get("usage_class") or "low", new_class,
            )
        except Exception as e:
            logger.warning(
                "usage_class: falha ao atualizar stack %s (%s)", row["stack_id"], e
            )


async def usage_class_loop(interval_s: float = USAGE_CLASS_INTERVAL_S):
    # primeira rodada logo após o boot (delay curto só pra não competir com o
    # startup): com sleep-first de 6h e redeploy a cada push na main, um
    # processo que nunca fica 6h de pé jamais classificaria stack nenhuma —
    # a classificação inteira virava no-op silencioso
    await asyncio.sleep(60.0)
    while True:
        try:
            await classify_stacks_once()
        except Exception as e:
            logger.warning("classificação periódica de usage_class falhou: %s", e)
        await asyncio.sleep(interval_s)


async def reconcile_stale_routes_once() -> None:
    """Recupera contas presas em routing_state.lora_status in
    ('loading','migrating') há mais tempo do que qualquer load/migração
    legítima levaria. claim_route/set_client_location só saem desses
    estados via código explícito de reversão (do_load falhar → mark_slot_idle;
    migrate falhar → volta pra 'loaded') — se ESSA própria chamada de
    reversão falhar (ex.: hiccup de rede pro Supabase bem nesse instante),
    a linha fica presa pra sempre: nada mais no sistema a revisita
    (list_idle_routes só olha 'loaded'), e todo request futuro da conta
    bate em wait_until_routed e recebe 503 "adapter carregando" sem nunca
    se recuperar sozinho. Reset pra 'unloaded' é seguro mesmo se a operação
    original eventualmente completasse tarde: o pior caso é um load/migração
    redundante no próximo request, não um estado inconsistente."""
    cutoff_iso = datetime.fromtimestamp(
        time.time() - STALE_ROUTE_THRESHOLD_S, tz=timezone.utc
    ).isoformat()
    try:
        stale = await store.list_stale_transitional_routes(cutoff_iso)
    except Exception as e:
        logger.warning("reconciliação de rotas presas: falha ao listar (%s)", e)
        return
    for route in stale:
        stack_id = route.get("stack_id")
        if not stack_id:
            continue
        try:
            await store.mark_slot_idle(stack_id)
            logger.warning(
                "reconciliação: stack %s presa em '%s' desde %s — liberada",
                stack_id, route.get("lora_status"), route.get("updated_at"),
            )
        except Exception as e:
            logger.warning("reconciliação de rotas presas: falha ao liberar %s (%s)", stack_id, e)


async def stale_route_reconciliation_loop(interval_s: float = STALE_ROUTE_CHECK_INTERVAL_S):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await reconcile_stale_routes_once()
        except Exception as e:
            logger.warning("reconciliação periódica de rotas presas falhou: %s", e)


async def machine_free_slots(machine: dict) -> int:
    """Slots LoRA livres da máquina.

    Slots por VRAM via machine_lora_slots() (mesma fórmula do painel);
    MAX_LORAS_PER_MACHINE atua como teto (espelho do --max-loras do pod) e
    como fallback quando a capacidade é desconhecida (sem VRAM/template).
    """
    by_vram = await supa.machine_lora_slots(machine["id"])
    slots = MAX_LORAS_PER_MACHINE if by_vram is None else min(by_vram, MAX_LORAS_PER_MACHINE)
    used = await supa.count_active_routes(machine["id"])
    return slots - used


def _forget_machine_upserts(machine_id: str) -> None:
    """Invalida o cache de upserts da máquina — o pod reiniciou e o agent
    voltou sem nenhuma chave em memória."""
    for k in [k for k in agent_key_upserts if k[1] == machine_id]:
        agent_key_upserts.pop(k, None)


def handle_machine_running(machine_id: str) -> None:
    """Callback do reconcile do lifecycle: máquina observada como promovida a
    running (religada pelo console do RunPod, recreateMachine, etc.) — o pod
    reiniciou com o agent zerado, então invalida o cache de upserts e agenda
    o reenvio das chaves."""
    _forget_machine_upserts(machine_id)
    schedule_key_sync(machine_id)


async def ensure_key_on_machine(entry: dict, machine: dict) -> None:
    """Garante a chave da conta no agent do pod antes do proxy.

    O agent perde as chaves em memória a cada restart do pod (stop/start) e,
    no fluxo base, a conta pode ser servida por uma máquina onde a chave
    nunca foi sincronizada (a chave é vinculada à máquina do stack) — sem o
    upsert, o pod rejeitaria com 401. Enquanto o pod boota, o call_agent
    devolve o 503 padrão e o retry do cliente converge sozinho."""
    cache_key = (entry["key_hash"], machine["id"])
    if agent_key_upserts.get(cache_key, 0) > time.time():
        return
    await call_agent(machine, "/upsert-keys", {"keys": [{
        "key_hash": entry["key_hash"],
        "api_key_id": entry.get("api_key_id"),
        "key_prefix": entry["key_prefix"],
        "account_name": entry["account_name"],
        "expires_at": entry.get("expires_at"),
    }]})
    agent_key_upserts[cache_key] = time.time() + UPSERT_CACHE_TTL_S


async def auto_provision_enabled() -> bool:
    """Interruptor liga/desliga do provisionamento automático (system_settings,
    controlado pelo painel). Cache curto em memória — mesmo padrão do
    key_cache, evita 1 round-trip ao Supabase por request na hot path sem
    deixar o toggle demorar minutos pra fazer efeito."""
    global auto_provision_cache
    now = time.time()
    if auto_provision_cache and auto_provision_cache[1] > now:
        return auto_provision_cache[0]
    try:
        value = await supa.get_setting("auto_provision_enabled", False)
    except Exception:
        value = False  # Supabase fora do ar: nunca provisiona por engano
    auto_provision_cache = (value, now + SETTINGS_CACHE_TTL_S)
    return value


async def wake_machine(machine: dict, reason: str) -> str:
    """Religa um pod pausado (startPod) e o devolve ao pool de roteamento.

    Retorna: 'woke' = startPod disparado agora; 'cooldown' = tentativa recente
    ainda no cooldown (não tenta de novo); 'no_gpu' = o host cedeu a GPU e o
    start é impossível até recriar o pod (o chamador dispara a recriação);
    'failed' = falha por outro motivo (ou sem runpod_client/pod).

    O touch de atividade vem ANTES do flip para running: sem ele, o
    last_activity_at velho faria a auto-pausa parar a máquina de novo no
    próximo ciclo, enquanto o vLLM ainda carrega o modelo."""
    if runpod_client is None or not machine.get("runpod_pod_id"):
        return "failed"
    now = time.time()
    if now - last_wake_attempt.get(machine["id"], 0) < WAKE_COOLDOWN_S:
        return "cooldown"
    # marca a tentativa antes do primeiro await — atômico dentro do event loop
    last_wake_attempt[machine["id"]] = now
    try:
        await runpod_client.start_pod(machine["runpod_pod_id"])
    except Exception as e:
        if "not enough free GPUs" in str(e):
            # host cedeu a GPU do pod pausado — religar nunca vai funcionar,
            # o chamador precisa recriar o pod num host novo
            logger.warning(
                "auto-wake: %s sem GPU no host, requer recriação (%s)", machine["id"], e
            )
            return "no_gpu"
        logger.warning("auto-wake: startPod de %s falhou (%s)", machine["id"], e)
        return "failed"
    try:
        await supa.touch_machine_activity(machine["id"])
    except Exception:
        pass
    await supa.set_machine_status(machine["id"], "running")
    # o pod reinicia com o agent zerado — invalida o cache de upserts e
    # agenda o reenvio das chaves assim que o vLLM ficar de pé
    _forget_machine_upserts(machine["id"])
    schedule_key_sync(machine["id"])
    try:
        await supa.log_machine_event(machine["id"], "started", f"Auto-wake: {reason}")
    except Exception:
        pass
    logger.info(
        "auto-wake: máquina %s (pod %s) religada — %s",
        machine["id"], machine["runpod_pod_id"], reason,
    )
    return "woke"


async def wake_some_machine_for_plan(plan: str) -> str:
    """Tenta pôr de pé alguma máquina pausada do template do plano. Só é chamado
    quando não há NENHUMA máquina running com vaga.

    Cascata por máquina pausada: religa (startPod); se o host cedeu a GPU
    ('no_gpu'), dispara a recriação num host novo. Retorna:
      - 'woke'      : despausou uma agora → cliente reintenta (religando);
      - 'recreating': nenhuma religou, mas disparamos/já há uma recriação →
                      cliente reintenta (recriando);
      - 'waking'    : há pausada subindo (cooldown de um wake recente bem
                      encaminhado), cliente reintenta;
      - 'none'      : não há pausada nenhuma (chamador decide provisionar)."""
    stopped = await supa.list_stopped_machines_for_plan(plan)
    if not stopped:
        return "none"
    recreating = False
    waking = False
    for m in stopped:
        if m["id"] in recreating_in_progress:
            recreating = True
            continue
        outcome = await wake_machine(m, "requisição recebida sem máquina disponível")
        if outcome == "woke":
            return "woke"
        if outcome == "no_gpu":
            if await try_recreate_machine(m, "host sem GPU pra religar sob demanda"):
                recreating = True
        elif outcome == "cooldown":
            # tentativa recente; se não caiu em recreate, tratamos como subindo
            waking = True
    if recreating:
        return "recreating"
    return "waking" if waking else "none"


def waking_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Sua máquina está sendo iniciada e ficará pronta em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": "60"},
    )


def provisioning_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Estamos preparando uma máquina nova para você — ficará pronta em "
        "instantes. Tente novamente em alguns segundos.",
        headers={"Retry-After": str(int(PROVISION_RETRY_AFTER_S))},
    )


def recreating_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Estamos recriando sua máquina e ela ficará pronta em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": str(int(RECREATE_RETRY_AFTER_S))},
    )


def preparing_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Estamos preparando sua máquina — ela ficará disponível em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": "30"},
    )


async def provision_machine_for_plan(plan: str) -> dict | None:
    """POST {PANEL_URL}/api/machines/provision — pede ao painel Next.js pra
    criar uma máquina nova do plano (o gateway nunca fala com a API de
    criação da RunPod diretamente, ver comentário das env vars no topo).
    None em qualquer falha (painel desligado/fora do ar, timeout, painel
    recusou) — o chamador decide o fallback, nunca propaga exceção."""
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        return None
    try:
        r = await panel_client.post(
            f"{PANEL_URL}/api/machines/provision",
            json={"plan": plan},
            headers={"X-Admin-Secret": PANEL_ADMIN_SECRET},
        )
    except httpx.HTTPError as e:
        logger.warning("provisionamento: chamada ao painel (%s) falhou (%s)", plan, e)
        return None
    if r.status_code != 200:
        logger.warning(
            "provisionamento: painel recusou %s (%s): %s", plan, r.status_code, r.text
        )
        return None
    return r.json()


async def recreate_machine_via_panel(machine_id: str) -> dict | None:
    """POST {PANEL_URL}/api/machines/{id}/recreate — pede ao painel pra recriar
    o pod num host novo (delete + create + start), mantendo a MESMA row de
    machines (stacks/chaves seguem apontando pra ela). Usado quando o auto-wake
    falhou por 'not enough free GPUs'. None em qualquer falha (painel desligado/
    fora do ar, timeout, recusa) — o chamador decide o fallback."""
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        return None
    try:
        r = await panel_client.post(
            f"{PANEL_URL}/api/machines/{machine_id}/recreate",
            headers={"X-Admin-Secret": PANEL_ADMIN_SECRET},
        )
    except httpx.HTTPError as e:
        logger.warning("recriação: chamada ao painel (%s) falhou (%s)", machine_id, e)
        return None
    if r.status_code != 200:
        logger.warning(
            "recriação: painel recusou %s (%s): %s", machine_id, r.status_code, r.text
        )
        return None
    return r.json()


async def _recreate_and_track(machine_id: str, reason: str) -> None:
    """Task de background: recria o pod e libera a trava ao fim. O request que
    disparou já respondeu 503 + Retry-After; o cliente reconverge quando o pod
    novo sobe (a reconciliação do gateway reenvia as chaves ao ficar running)."""
    try:
        result = await recreate_machine_via_panel(machine_id)
        if result is None:
            logger.warning(
                "recriação de %s não completou (%s) — fica na fila pro lifecycle retentar",
                machine_id, reason,
            )
        else:
            pending_recreates.discard(machine_id)  # sucesso: sai da fila de retry
            logger.info("recriação de %s disparada — %s", machine_id, reason)
    finally:
        recreating_in_progress.discard(machine_id)


async def try_recreate_machine(machine: dict, reason: str) -> bool:
    """Dispara a recriação em background se o painel estiver configurado, não
    houver uma recriação em andamento pra essa máquina e o cooldown já tiver
    passado. Retorna True se há recriação encaminhada (disparada agora, já em
    andamento, ou recente dentro do cooldown) — o chamador levanta
    recreating_503(). Não fica atrás de auto_provision_enabled: recriar restaura
    uma máquina que o usuário já provisionou (o host cedeu a GPU), não cria
    capacidade nova.

    Enfileira a máquina em pending_recreates: se a chamada ao painel falhar (ou
    o processo cair antes de concluir), o lifecycle loop retenta. A entrada só
    sai da fila quando uma recriação conclui com sucesso."""
    machine_id = machine["id"]
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        return False
    pending_recreates.add(machine_id)
    if machine_id in recreating_in_progress:
        return True
    now = time.time()
    if now - last_recreate_attempt.get(machine_id, 0) < RECREATE_COOLDOWN_S:
        # recriação recente já disparada — o pod novo está subindo
        return True
    # checagem + marcação sem await no meio (atômicas dentro do event loop,
    # mesma disciplina do provisioning_in_progress)
    last_recreate_attempt[machine_id] = now
    recreating_in_progress.add(machine_id)
    asyncio.create_task(_recreate_and_track(machine_id, reason))
    return True


async def _wait_machine_healthy(
    machine_id: str, timeout_s: float, poll_interval_s: float = 10.0
) -> bool:
    """Poll em GET {public_url}/health (sem auth — endpoint do agent) até o
    vLLM confirmar modelo carregado (vllm_ready) ou o timeout esgotar."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        machine = await supa.get_machine(machine_id)
        if machine and machine.get("public_url"):
            try:
                r = await proxy_client.get(
                    f"{machine['public_url']}/health",
                    timeout=httpx.Timeout(5.0, connect=5.0),
                )
                if r.status_code == 200 and r.json().get("vllm_ready"):
                    return True
            except Exception:
                pass
        await asyncio.sleep(poll_interval_s)
    return False


def schedule_key_sync(machine_id: str) -> None:
    """Agenda (fire-and-forget) o reenvio das chaves da máquina ao agent
    assim que o pod ficar saudável — o agent volta de qualquer restart com
    zero chaves em memória; sem isso, todo request pós-religada vira 401 até
    um sync manual. Checagem + marcação sem await no meio (atômicas dentro
    do event loop, mesma disciplina do provisioning_in_progress)."""
    if machine_id in key_sync_in_progress:
        return
    key_sync_in_progress.add(machine_id)
    asyncio.create_task(_sync_keys_when_healthy(machine_id))


async def _sync_keys_when_healthy(machine_id: str) -> None:
    """Task de background: espera o vLLM de pé e reenvia em lote todas as
    chaves ativas da máquina. Usa /upsert-keys (não /sync-keys) pra nunca
    clobber chaves que o fluxo LoRA/base upsertou enquanto o lote esperava.
    Nunca deixa exceção escapar (fire-and-forget)."""
    try:
        healthy = await _wait_machine_healthy(
            machine_id, MACHINE_HEALTH_TIMEOUT_S, MACHINE_HEALTH_POLL_INTERVAL_S
        )
        if not healthy:
            logger.warning("key-sync: máquina %s não ficou saudável a tempo", machine_id)
            return
        machine = await supa.get_machine(machine_id)
        if not machine or not machine.get("public_url"):
            return
        keys = await supa.list_active_keys_for_machine(machine_id)
        if keys:
            await call_agent(machine, "/upsert-keys", {"keys": keys})
        try:
            await supa.log_machine_event(
                machine_id, "sync", f"{len(keys)} chave(s) reenviada(s) após religar"
            )
        except Exception:
            pass
        logger.info("key-sync: %d chave(s) reenviada(s) para %s", len(keys), machine_id)
    except Exception as e:
        logger.warning("key-sync: reenvio de chaves para %s falhou (%s)", machine_id, e)
    finally:
        key_sync_in_progress.discard(machine_id)


async def _provision_and_track(plan: str, reason: str, pause_when_healthy: bool) -> None:
    """Task de background: cria -> espera saudável -> opcionalmente pausa
    (reposição proativa) — nunca deixa exceção escapar (fire-and-forget)."""
    try:
        machine = await provision_machine_for_plan(plan)
        if not machine:
            return
        try:
            await supa.log_machine_event(
                machine["machine_id"], "created", f"Provisionamento automático: {reason}"
            )
        except Exception:
            pass
        healthy = await _wait_machine_healthy(
            machine["machine_id"], MACHINE_HEALTH_TIMEOUT_S, MACHINE_HEALTH_POLL_INTERVAL_S
        )
        if healthy and pause_when_healthy and runpod_client is not None:
            m = await supa.get_machine(machine["machine_id"])
            if m and m.get("runpod_pod_id"):
                try:
                    await runpod_client.stop_pod(m["runpod_pod_id"])
                    await supa.set_machine_status(machine["machine_id"], "stopped")
                    await supa.log_machine_event(
                        machine["machine_id"], "stopped",
                        "Reposição proativa: pausada assim que ficou saudável",
                    )
                except Exception as e:
                    logger.warning(
                        "provisionamento: pausa pós-boot de %s falhou (%s)",
                        machine["machine_id"], e,
                    )
    except Exception as e:
        logger.warning("provisionamento automático (%s) falhou (%s)", plan, e)
    finally:
        provisioning_in_progress.discard(plan)


async def _try_provision_machine_for_plan(
    plan: str, reason: str, pause_when_healthy: bool
) -> bool:
    """Dispara a criação em background se o interruptor estiver ligado, o
    painel estiver configurado, não houver uma criação em andamento pro
    plano e o cooldown já tiver passado. Fonte única de verdade do
    interruptor — os 3 chamadores (cascata reativa x2, ensure_capacity_once)
    não precisam checar auto_provision_enabled() cada um por conta própria.
    A decisão de SE vale a pena criar (dado que está ligado) é toda do
    chamador — aqui só há as travas."""
    if not await auto_provision_enabled():
        return False
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        # sem painel configurado, provision_machine_for_plan sempre devolve
        # None — sem essa checagem aqui, o chamador levantaria um
        # provisioning_503() mentiroso (promete retry, mas nunca vai criar)
        return False
    if plan in provisioning_in_progress:
        return False
    now = time.time()
    if now - last_provision_attempt.get(plan, 0) < PROVISION_COOLDOWN_S:
        return False
    # daqui pra baixo não há mais nenhum await antes de marcar a trava —
    # checagem + marcação são atômicas dentro do event loop (mesmo cuidado
    # do wake_machine existente)
    last_provision_attempt[plan] = now
    provisioning_in_progress.add(plan)
    asyncio.create_task(_provision_and_track(plan, reason, pause_when_healthy))
    return True


async def try_provision_for_request(plan: str, reason: str) -> bool:
    """Cascata reativa (3º nível): não pausa ao ficar saudável — o próprio
    request que disparou precisa da máquina de pé pro retry."""
    return await _try_provision_machine_for_plan(plan, reason, pause_when_healthy=False)


async def try_provision_for_pool(plan: str, reason: str) -> bool:
    """Reposição proativa: pausa ao ficar saudável — ninguém está esperando,
    minimiza custo de GPU ociosa."""
    return await _try_provision_machine_for_plan(plan, reason, pause_when_healthy=True)


async def pick_machine_with_free_slot(plan: str) -> dict:
    """Alocação placeholder: primeira máquina running (do template do plano
    da conta) com slot LoRA livre. Sem capacidade → tenta religar um pod
    pausado do plano (auto-wake); sem pausada, tenta provisionar uma nova
    (3º nível, se o interruptor estiver ligado) antes de desistir."""
    machines = await supa.list_running_machines_for_plan(plan)
    for m in machines:
        if await machine_free_slots(m) > 0:
            return m
    woke = await wake_some_machine_for_plan(plan)
    if woke == "recreating":
        raise recreating_503()
    if woke != "none":
        raise waking_503()
    if plan in provisioning_in_progress or await try_provision_for_request(
        plan, "sem máquina com vaga nem pausada"
    ):
        raise provisioning_503()
    if not machines:
        raise HTTPException(status_code=503, detail="nenhuma máquina disponível")
    raise HTTPException(status_code=503, detail="sem capacidade: todas as máquinas estão cheias")


async def do_load(stack_id: str, entry: dict, machine: dict, adapter: dict) -> None:
    """Garante a chave no agent, baixa+carrega o adapter e confirma a rota.
    Escopo por stack: o adapter é nomeado e roteado por stack_id (a chave, que é
    por conta, é garantida separadamente por ensure_key_on_machine)."""
    await ensure_key_on_machine(entry, machine)
    files = await supa.signed_lora_files(adapter["storage_path"])
    await call_agent(
        machine, "/load-lora",
        {"lora_name": lora_name(stack_id), "files": files},
        timeout_s=LORA_LOAD_TIMEOUT_S,
    )
    await store.set_client_location(
        stack_id,
        machine_id=machine["id"],
        lora_adapter_id=adapter["id"],
        lora_status="loaded",
    )


async def wait_until_routed(stack_id: str) -> dict | None:
    """Espera (poll curto) um load em andamento de outro request terminar."""
    deadline = time.time() + LOAD_WAIT_TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        route = await store.get_client_location(stack_id)
        if route and route["lora_status"] in ("loaded", "migrating") and route["machine_id"]:
            return route
        if not route or route["lora_status"] == "unloaded":
            return None  # o load falhou e o slot foi liberado
    return None


def resolve_key_stack(entry: dict) -> tuple[dict | None, str | None]:
    """Stack efetiva da chave e o plano dela pro resto do fluxo.

    Plano é propriedade da stack (migration 0027 removeu accounts.plan — uma
    conta pode ter stacks de planos diferentes, então não existe mais "o
    plano da conta"). Chave sem `stack_id` resolvível só passa por
    `authenticate` quando a conta não tem NENHUMA stack — nesse caso não há
    plano nenhum pra usar; devolve (None, None) e quem chama trata como
    "conta sem stack configurada" em vez de adivinhar."""
    stack_id = entry.get("stack_id")
    if stack_id:
        stack = next((s for s in entry.get("stacks") or [] if s["id"] == stack_id), None)
        if stack:
            return stack, stack["plan"]
    return None, None


async def pick_running_machine_with_stack_slot(
    plan: str, exclude_machine_id: str | None = None, required_weight: float = 1.0
) -> dict | None:
    """Primeira máquina running do plano com vaga de stack. Ocupação
    PONDERADA pela classe de uso (migration 0032): a vaga precisa comportar o
    PESO do entrante (low=1.0 preserva o antigo "1 stack = 1 slot"; um high
    custa 3.0 — evita empilhar usuários de contexto longo na mesma máquina).
    Capacidade desconhecida/sem teto (slots 0) é aceita — mesmo critério do
    allocateMachineForTemplate do painel (lib/actions.ts)."""
    for m in await supa.list_running_machines_for_plan(plan):
        if exclude_machine_id and m["id"] == exclude_machine_id:
            continue
        slots = await supa.machine_stack_slots(m["id"])
        if slots is None or slots == 0:
            return m
        if slots - await supa.machine_stack_load(m["id"]) >= required_weight:
            return m
    return None


async def reallocate_stack(entry: dict, stack: dict, old_machine: dict) -> dict | None:
    """Realocação automática (cenário: máquina do stack pausada/terminada e
    o usuário mandou request): muda a "casa" do usuário DE VEZ pra uma
    running com vaga — stacks.machine_id reponta e as chaves ativas da conta
    MOVEM junto (api_keys.machine_id); a plain key do cliente continua a
    mesma, diferente do migrateStack do painel, que cria/revoga chaves.
    None = sem vaga em lugar nenhum ou perdeu a corrida (o chamador decide:
    religar a própria máquina ou cair no fallback por plano).

    Limitação aceita: com múltiplos stacks da conta na mesma origem, só o
    stack escolhido reponta (as chaves movem juntas); os irmãos ficam para o
    admin migrar via migrateStack.
    """
    moved = False
    async with realloc_locks[stack["plan"]]:
        fresh = await supa.get_stack(stack["id"])
        if not fresh:
            return None
        if fresh["machine_id"] != old_machine["id"]:
            # request concorrente já realocou — segue a máquina nova dele
            if not fresh["machine_id"]:
                return None
            m = await supa.get_machine(fresh["machine_id"])
            if not (m and m.get("status") == "running" and m.get("public_url")):
                return None
            target = m
        else:
            target = await pick_running_machine_with_stack_slot(
                stack["plan"],
                exclude_machine_id=old_machine["id"],
                # a vaga precisa comportar o peso REAL da stack que chega —
                # um usuário high (contexto longo) custa 3.0, não 1.
                # Limitação conhecida: usa os pesos DEFAULT (sem o override de
                # templates.usage_class_config, que o machine_stack_load do
                # destino aplica) — só divergiria se algum template definisse
                # "weights" custom, o que nenhum faz hoje
                required_weight=class_weight(fresh.get("usage_class")),
            )
            if not target:
                return None
            if not await supa.repoint_stack(stack["id"], old_machine["id"], target["id"]):
                return None
            await supa.move_account_keys(
                entry["account_id"], old_machine["id"], target["id"],
                stack_id=stack["id"] if entry.get("stack_id") else None,
            )
            moved = True

    # stack é o mesmo objeto guardado no key_cache — mutar in place mantém o
    # cache coerente pelo resto do TTL sem flush
    stack["machine_id"] = target["id"]
    agent_key_upserts.pop((entry["key_hash"], old_machine["id"]), None)
    await ensure_key_on_machine(entry, target)
    if moved:
        try:
            await store.record_reallocation(
                entry["account_id"],
                from_machine_id=old_machine["id"],
                machine_id=target["id"],
            )
            reason = {"stopped": "pausada", "terminated": "terminada"}.get(
                old_machine.get("status"), "indisponível"
            )
            await supa.log_machine_event(
                target["id"], "stack_migrated",
                f"Stack {stack.get('slug') or stack['id']} realocada automaticamente "
                f"({old_machine.get('name') or 'origem'} {reason})",
            )
        except Exception:
            pass  # histórico é best-effort, nunca derruba o request
        logger.info(
            "realloc: stack %s da conta %s movida de %s para %s",
            stack.get("slug") or stack["id"], entry["account_id"],
            old_machine["id"], target["id"],
        )
    return target


async def place_base_stack(entry: dict, stack: dict) -> dict | None:
    """Re-aloca a "casa" de uma stack de modelo base que teve o slot liberado
    por ociosidade (stacks.machine_id == NULL, zerado pelo reap_idle_base_stacks
    do lifecycle). Escolhe uma máquina running do MESMO plano com vaga
    PONDERADA pela classe de uso (baixo/médio/alto), não necessariamente a
    anterior. Irmão enxuto do reallocate_stack, sem origem conhecida.

    None = sem vaga em máquina nenhuma (o chamador cai no wake/provision por
    plano) ou perdeu a corrida sem a máquina do vencedor estar pronta.
    """
    async with realloc_locks[stack["plan"]]:
        fresh = await supa.get_stack(stack["id"])
        if not fresh:
            return None
        if fresh.get("machine_id"):
            # request concorrente já re-homeou — segue a máquina nova dele
            m = await supa.get_machine(fresh["machine_id"])
            if m and m.get("status") == "running" and m.get("public_url"):
                stack["machine_id"] = m["id"]
                await ensure_key_on_machine(entry, m)
                return m
            return None
        target = await pick_running_machine_with_stack_slot(
            stack["plan"],
            required_weight=class_weight(fresh.get("usage_class")),
        )
        if not target:
            return None
        if not await supa.repoint_stack_from_null(stack["id"], target["id"]):
            return None
        await supa.rebind_stack_keys(entry["account_id"], target["id"], stack["id"])

    # stack é o mesmo objeto do key_cache — mutar in place mantém o cache
    # coerente pelo resto do TTL sem flush (igual ao reallocate_stack)
    stack["machine_id"] = target["id"]
    await ensure_key_on_machine(entry, target)
    try:
        await supa.log_machine_event(
            target["id"], "stack_placed",
            f"Stack {stack.get('slug') or stack['id']} re-alocada após ociosidade",
        )
    except Exception:
        pass  # histórico é best-effort, nunca derruba o request
    logger.info(
        "place_base_stack: stack %s da conta %s re-alocada em %s",
        stack.get("slug") or stack["id"], entry["account_id"], target["id"],
    )
    return target


async def resolve_base_machine(account_id: str, entry: dict) -> tuple[dict, str]:
    """Máquina pro modelo base (conta sem adapter), stack-aware. Retorna
    (machine, effective_plan) — effective_plan é sempre o plano da STACK
    resolvida pela chave (ver resolve_key_stack; chamador já garantiu que
    não é None antes de chegar aqui).

    1. Máquina do stack running → serve nela (chave garantida via upsert
       lazy — o pod pode ter reiniciado e perdido as chaves).
    2. Pausada/terminada/erro → realoca o stack pra outra running com vaga
       (permanente). Sem vaga e pausada → religa a PRÓPRIA máquina do
       usuário (as chaves já estão vinculadas a ela) e responde 503 +
       Retry-After pro retry do cliente.
    3. Sem stack, stack sem máquina, ou wake da própria falhou (ex.: host
       sem GPU livre) → fallback por plano (comportamento original), agora
       com upsert lazy da chave — sem ele o pod da outra máquina rejeitaria
       a chave com 401.
    """
    stack, effective_plan = resolve_key_stack(entry)
    if stack and stack.get("machine_id"):
        machine = await supa.get_machine(stack["machine_id"])
        if machine:
            status = machine.get("status")
            if status == "running" and machine.get("public_url"):
                await ensure_key_on_machine(entry, machine)
                return machine, effective_plan
            if status in ("stopped", "terminated", "error"):
                target = await reallocate_stack(entry, stack, machine)
                if target:
                    return target, effective_plan
                if status == "stopped":
                    slug = stack.get("slug") or stack["id"]
                    if machine["id"] in recreating_in_progress:
                        # recriação disparada por um request anterior ainda em
                        # curso — o pod novo está subindo
                        raise recreating_503()
                    outcome = await wake_machine(
                        machine, f"stack {slug}: máquina pausada e sem vaga nas demais"
                    )
                    if outcome in ("woke", "cooldown"):
                        # 'cooldown' = request concorrente já disparou o wake e o
                        # pod está subindo — não religa uma 2ª máquina à toa
                        raise waking_503()
                    fresh = await supa.get_machine(machine["id"])
                    if fresh and fresh.get("status") == "running":
                        raise waking_503()
                    if outcome == "no_gpu" and await try_recreate_machine(
                        machine, f"stack {slug}: host sem GPU pra religar"
                    ):
                        # host cedeu a GPU do pod pausado → recria num host novo
                        raise recreating_503()
                    # wake e recreate não resolveram → fallback por plano:
                    # serve temporário sem mover o stack
    elif stack and not stack.get("machine_id"):
        # casa liberada por ociosidade (reap_idle_base_stacks zerou machine_id)
        # ou stack nunca homeada → re-aloca DE VEZ numa máquina do plano com
        # vaga ponderada, em vez de cair no fallback "primeira máquina" (que
        # servia sem contabilizar a vaga e sem re-homear)
        target = await place_base_stack(entry, stack)
        if target:
            return target, effective_plan
        # sem vaga em lugar nenhum → cai no fallback (wake/provision por plano)

    machines = await supa.list_running_machines_for_plan(effective_plan)
    if not machines:
        woke = await wake_some_machine_for_plan(effective_plan)
        if woke == "recreating":
            raise recreating_503()  # host sem GPU → recriando num host novo
        if woke != "none":
            raise waking_503()  # despausando agora ou uma já está subindo
        if effective_plan in provisioning_in_progress or await try_provision_for_request(
            effective_plan, "sem máquina para o modelo base"
        ):
            raise provisioning_503()
        raise preparing_503()
    machine = machines[0]
    await ensure_key_on_machine(entry, machine)
    return machine, effective_plan


async def resolve_route(account_id: str, entry: dict) -> tuple[dict, bool, str, str]:
    """Resolve (machine, rewrite_model, effective_plan, stack_id) para a conta.

    O roteamento é escopado por STACK (migration 0029): a rota, o nome do
    adapter e o in_flight/drain são por stack_id (resolvido da própria chave via
    resolve_key_stack). account_id ainda circula para chaves e histórico.

    Regra primária: rota com machine_id e status loaded/migrating → proxy
    direto (migrating = origem continua servindo). Sem adapter registrado →
    modelo base numa máquina running, sem reescrever "model".

    `effective_plan` vem sempre de `resolve_key_stack` — plano é propriedade
    da stack da própria chave (migration 0027 removeu accounts.plan), tanto
    nos branches de adapter LoRA quanto no modelo base. Adapter LoRA também
    é resolvido por stack (`latest_ready_adapter_for_stack`, migration 0026)
    — cada stack pode ter (ou não) seu próprio fine-tune.
    """
    stack, effective_plan = resolve_key_stack(entry)
    if effective_plan is None:
        raise HTTPException(status_code=503, detail="conta sem stack configurada")
    stack_id = stack["id"]

    route = await store.get_client_location(stack_id)

    if route and route["machine_id"] and route["lora_status"] in ("loaded", "migrating"):
        machine = await supa.get_machine(route["machine_id"])
        if not machine:
            raise HTTPException(status_code=503, detail="máquina da rota não existe mais")
        # "stopped": stop manual pelo painel com a rota ainda apontando pra
        # máquina (a auto-pausa exige 0 rotas, então nunca cria este estado
        # sozinha). "terminated"/"error": pod sumiu de vez (deletado via
        # console do RunPod, host reclamou a instância) —
        # reconcile_statuses_once promove pra esses status SEM checar rotas
        # ativas (ao contrário de stop_idle_machines_once). Sem public_url:
        # estado transitório/inconsistente, não dá pra servir mesmo com
        # status "running". Em qualquer um desses casos o adapter não está
        # mais carregado (nem nunca mais vai estar, nos dois primeiros) —
        # sem tratar isso aqui, a conta ficava presa apontando pra uma
        # máquina morta em todo request seguinte, sem nenhuma auto-cura.
        if machine.get("status") in ("stopped", "terminated", "error") or not machine.get(
            "public_url"
        ):
            await store.mark_slot_idle(stack_id)
            route = None
        else:
            return machine, True, effective_plan, stack_id

    if route and route["lora_status"] == "loading":
        waited = await wait_until_routed(stack_id)
        if waited:
            machine = await supa.get_machine(waited["machine_id"])
            if machine:
                return machine, True, effective_plan, stack_id
        raise HTTPException(
            status_code=503,
            detail="adapter carregando, tente novamente",
            headers={"Retry-After": "5"},
        )

    # sem rota ativa: a STACK da chave tem adapter?
    adapter = await supa.latest_ready_adapter_for_stack(stack_id)
    if not adapter:
        # sem adapter registrado → serve o modelo base, stack-aware: máquina
        # da stack da chave quando running; pausada → realocação automática
        # ou wake da própria; fallback por plano (VibeCoder nunca cai numa
        # máquina servindo o modelo do Pro/Max, e vice-versa)
        machine, effective_plan = await resolve_base_machine(account_id, entry)
        return machine, False, effective_plan, stack_id

    machine = await pick_machine_with_free_slot(effective_plan)
    result = await store.claim_client_location(stack_id, account_id, machine["id"])
    if not result["claimed"]:
        # outro request da mesma stack venceu a corrida — espera o load dele
        waited = await wait_until_routed(stack_id)
        if waited:
            m = await supa.get_machine(waited["machine_id"])
            if m:
                return m, True, effective_plan, stack_id
        raise HTTPException(
            status_code=503,
            detail="adapter carregando, tente novamente",
            headers={"Retry-After": "5"},
        )

    try:
        await do_load(stack_id, entry, machine, adapter)
    except HTTPException:
        await store.mark_slot_idle(stack_id)
        raise
    except Exception as e:
        await store.mark_slot_idle(stack_id)
        raise HTTPException(status_code=503, detail=f"falha ao carregar adapter: {e}")
    return machine, True, effective_plan, stack_id


# ---------- Proxy ----------


async def maybe_touch(stack_id: str, machine_id: str | None = None):
    now = time.time()
    if now - last_touch.get(stack_id, 0) >= TOUCH_THROTTLE_S:
        last_touch[stack_id] = now
        try:
            await store.touch(stack_id)
        except Exception:
            pass  # touch é best-effort, nunca derruba o request
    # atividade por máquina (base da auto-pausa) — cobre também requests de
    # modelo base sem rota, que não tocam routing_state
    if machine_id and now - last_machine_touch.get(machine_id, 0) >= TOUCH_THROTTLE_S:
        last_machine_touch[machine_id] = now
        try:
            await supa.touch_machine_activity(machine_id)
        except Exception:
            pass
    # atividade por stack (relógio de ociosidade do modelo base) — mantém a
    # stack "fresca" pra não ser reapada; vale pra base E LoRA, já que a stack
    # LoRA também tem stacks.machine_id como casa de fallback
    if now - last_stack_touch.get(stack_id, 0) >= TOUCH_THROTTLE_S:
        last_stack_touch[stack_id] = now
        try:
            await supa.touch_stack_activity(stack_id)
        except Exception:
            pass  # touch é best-effort, nunca derruba o request


async def embed_query(text: str) -> list[float] | None:
    """Embedding da última mensagem do usuário, pro retrieval do RAG.
    None em qualquer falha (sem OPENAI_API_KEY, API fora do ar, etc.) —
    RAG é best-effort, nunca derruba o request de inferência."""
    if not OPENAI_API_KEY:
        return None
    try:
        r = await openai_client.post(
            "/embeddings", json={"model": EMBEDDING_MODEL, "input": text}
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception:
        return None


MIN_MAX_TOKENS = 8000  # thinking mode (Qwen3.x) corta o raciocínio no meio quando o
# cliente manda um teto baixo — comum em ferramentas de terceiro (Cursor, Continue,
# Cline etc.) que fixam max_tokens curto por padrão. Como o produto é BYOE (o usuário
# aponta a ferramenta dele direto pro endpoint, sem UI de chat própria controlando
# esse parâmetro), o gateway impõe o piso aqui pra garantir qualidade consistente
# independente do cliente.
MAX_MAX_TOKENS = int(os.environ.get("MAX_MAX_TOKENS", "16000"))  # teto: sem isso um
# cliente podia pedir max_tokens arbitrário e a GPU rodava até esgotar o contexto,
# sem nenhum controle de custo (ver também check_concurrency/RATE_LIMIT_RPM)

ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


def pin_model(body_json: dict, stack_id: str, rewrite_model: bool, machine: dict) -> None:
    """Trava o campo "model": nunca confia no que o cliente mandou. Stack com
    adapter LoRA -> nome do adapter da PRÓPRIA stack (antes disso, além do
    cross-tenant, duas stacks da mesma conta colidiam no mesmo nome de adapter);
    stack base -> served_model_name da máquina (o alias de --served-model-name,
    ex.: "pro-base"). É o ÚNICO nome que o vLLM aceita quando o template define
    esse alias; fixar o machines.model_name (path do HF, ex.: "Qwen/...") daria
    404 "model does not exist". Fallback pro model_name quando o template não usa
    a flag (aí o vLLM serve pelo próprio --model).
    Roda sempre, mesmo se o cliente omitiu "model" ou mandou um --model
    diferente na CLI dele (Codex/Claude Code guardam isso em config local, que
    não temos como fiscalizar — a única trava confiável é no servidor)."""
    if rewrite_model:
        body_json["model"] = lora_name(stack_id)
    else:
        body_json["model"] = machine.get("served_model_name") or machine.get("model_name")


async def validate_body(
    body_json: dict, entry: dict, rewrite_model: bool, machine: dict, stack_id: str
) -> dict:
    """Ponto único de validação/transformação do corpo antes do proxy:
    trava o modelo, aplica piso/teto de max_tokens e clamp de parâmetros
    (qualquer endpoint /v1/*) e, só para chat completions (body com
    "messages"), filtra roles e injeta system prompt da stack + RAG."""
    pin_model(body_json, stack_id, rewrite_model, machine)

    current_max_tokens = body_json.get("max_tokens")
    if not isinstance(current_max_tokens, int) or current_max_tokens < MIN_MAX_TOKENS:
        body_json["max_tokens"] = MIN_MAX_TOKENS
    elif current_max_tokens > MAX_MAX_TOKENS:
        body_json["max_tokens"] = MAX_MAX_TOKENS

    # n>1 multiplica o custo de GPU por resposta — sem valor pro caso de uso
    # BYOE (ferramentas de código pedem 1 completion) e sem teto era um vetor
    # de abuso trivial (n=100 = 100x o custo de uma request só)
    if isinstance(body_json.get("n"), int):
        body_json["n"] = 1
    for param, lo, hi in (
        ("temperature", 0.0, 2.0),
        ("top_p", 0.0, 1.0),
        ("frequency_penalty", -2.0, 2.0),
        ("presence_penalty", -2.0, 2.0),
    ):
        value = body_json.get(param)
        if isinstance(value, (int, float)):
            body_json[param] = min(max(value, lo), hi)
    body_json.pop("logit_bias", None)

    messages = body_json.get("messages")
    if not isinstance(messages, list):
        # /v1/completions (prompt cru, sem messages): mesmo orçamento de
        # janela do chat — embeddings e afins não têm max_tokens e o
        # apply_context_budget vira no-op de clamp neles
        if isinstance(body_json.get("prompt"), str):
            prompt_text = body_json["prompt"]
            heuristic_est = estimate_prompt_tokens(extra_texts=[prompt_text])
            est_tokens = await resolve_est_tokens(machine, heuristic_est, prompt_text)
            apply_context_budget(body_json, machine, est_tokens=est_tokens)
        return body_json

    if len(messages) > MAX_MESSAGES:
        raise HTTPException(status_code=400, detail="número de mensagens excede o limite")

    # roles fora da whitelist são descartadas silenciosamente — nenhuma
    # ferramenta BYOE legítima deveria mandar algo além disso, e um role
    # desconhecido não tem tratamento definido no chat template do vLLM
    messages = [m for m in messages if m.get("role") in ALLOWED_ROLES]

    # normaliza "system": no máximo UM, sempre no índice 0 — o chat template
    # do Qwen3.x rejeita ("System message must be at the beginning") qualquer
    # role "system" que não seja a primeira mensagem. Se o cliente já mandou
    # um (comum em ferramentas BYOE — Cursor/Codex/Claude Code embutem o
    # próprio system prompt pra tool-calling/formatação), respeita o dele e
    # NÃO injeta o da stack — evita duas inserções (uma em 0, outra antes do
    # último user) que quebravam a chamada inteira, e preserva o
    # funcionamento normal da ferramenta cliente. Sem system do cliente,
    # injeta o system_prompt da stack + contexto do RAG.
    client_systems = [m for m in messages if m.get("role") == "system"]
    messages = [m for m in messages if m.get("role") != "system"]

    if client_systems:
        content = "\n\n---\n\n".join(
            c["content"] for c in client_systems if isinstance(c.get("content"), str)
        )
        if content:
            messages.insert(0, {"role": "system", "content": content})
    else:
        system_message = await build_stack_system_message(messages, entry)
        if system_message:
            messages.insert(0, system_message)

    body_json["messages"] = messages

    # Clamp dinâmico pela janela real do modelo — por último, com o prompt
    # FINAL (system da stack + RAG já injetados). Pode reduzir max_tokens
    # abaixo de MIN_MAX_TOKENS: entre truncar thinking e devolver o 400 cru
    # do vLLM, truncar é a degradação aceitável (o filtro de <think> tem
    # fallback pra stream cortado por length).
    tools = body_json.get("tools")
    heuristic_est = estimate_prompt_tokens(messages=messages, tools=tools)
    exact_text = prompt_text_for_tokenize(messages=messages, tools=tools)
    est_tokens = await resolve_est_tokens(machine, heuristic_est, exact_text)
    apply_context_budget(body_json, machine, est_tokens=est_tokens)
    return body_json


async def build_stack_system_message(messages: list, entry: dict) -> dict | None:
    """system_prompt configurado da STACK + contexto de RAG (top-k da base de
    conhecimento da STACK), pra quando o cliente não mandou system próprio.

    Reaproveita resolve_key_stack (mesmo helper do roteamento de máquina,
    commit 7e64aa4) para saber qual stack da conta está servindo o request —
    sem isso, contas com múltiplas stacks vazavam o mesmo prompt/RAG entre
    todas elas (system_prompt/knowledge_chunks eram só por account_id)."""
    stack, _ = resolve_key_stack(entry)

    system_parts = []
    # system_prompt é propriedade da stack (migration 0020); accounts.system_prompt
    # foi removida na 0027 — sem stack resolvida, não há prompt nenhum pra injetar.
    system_prompt = (stack or {}).get("system_prompt")
    if system_prompt:
        system_parts.append(system_prompt)

    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"), None
    )
    if last_user and isinstance(last_user.get("content"), str):
        embedding = await embed_query(last_user["content"])
        if embedding:
            chunks = await supa.match_knowledge_chunks(
                entry["account_id"],
                stack["id"] if stack else None,
                embedding,
                RAG_TOP_K,
            )
            if chunks:
                system_parts.append(
                    "Contexto relevante da base de conhecimento:\n"
                    + "\n---\n".join(chunks)
                )

    if not system_parts:
        return None
    return {"role": "system", "content": "\n\n---\n\n".join(system_parts)}


async def build_stack_instructions(entry: dict, last_user_text: str | None) -> str | None:
    """Equivalente a build_stack_system_message, mas devolve só o texto: a
    Responses API (Codex) usa o campo "instructions", não uma mensagem
    role=system dentro de "input"."""
    stack, _ = resolve_key_stack(entry)

    system_parts = []
    system_prompt = (stack or {}).get("system_prompt")
    if system_prompt:
        system_parts.append(system_prompt)

    if last_user_text:
        embedding = await embed_query(last_user_text)
        if embedding:
            chunks = await supa.match_knowledge_chunks(
                entry["account_id"],
                stack["id"] if stack else None,
                embedding,
                RAG_TOP_K,
            )
            if chunks:
                system_parts.append(
                    "Contexto relevante da base de conhecimento:\n"
                    + "\n---\n".join(chunks)
                )

    if not system_parts:
        return None
    return "\n\n---\n\n".join(system_parts)


def _last_user_text_from_responses_input(input_items) -> str | None:
    if not isinstance(input_items, list):
        return None
    for item in reversed(input_items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                part.get("text") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)
    return None


async def validate_responses_body(
    body_json: dict, entry: dict, rewrite_model: bool, machine: dict, stack_id: str
) -> dict:
    """Equivalente a validate_body pro formato da Responses API (Codex CLI):
    "input" no lugar de "messages", "instructions" no lugar de um system
    message, "max_output_tokens" no lugar de "max_tokens"."""
    pin_model(body_json, stack_id, rewrite_model, machine)

    # nunca persistir a resposta recuperável por outro tenant via
    # GET /v1/responses/{id} — esse subpath nem está na allowlist, mas
    # store=false garante que não sobra nada pra recuperar de qualquer jeito
    body_json["store"] = False

    max_output_tokens = body_json.get("max_output_tokens")
    if not isinstance(max_output_tokens, int) or max_output_tokens < MIN_MAX_TOKENS:
        body_json["max_output_tokens"] = MIN_MAX_TOKENS
    elif max_output_tokens > MAX_MAX_TOKENS:
        body_json["max_output_tokens"] = MAX_MAX_TOKENS

    for param, lo, hi in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        value = body_json.get(param)
        if isinstance(value, (int, float)):
            body_json[param] = min(max(value, lo), hi)

    input_items = body_json.get("input")
    if isinstance(input_items, list):
        if len(input_items) > MAX_MESSAGES:
            raise HTTPException(status_code=400, detail="número de itens excede o limite")
        # bug conhecido do Codex (openai/codex#12669): ao reenviar itens de
        # turnos anteriores (mensagens do assistente, chamadas de
        # ferramenta), às vezes vêm sem "id"/"status" — a validação estrita
        # do vLLM rejeita com 502. Sintetiza os dois só nesses itens (nunca
        # em input NOVO do usuário, que legitimamente não tem esses campos)
        for i, item in enumerate(input_items):
            if not isinstance(item, dict):
                continue
            is_replayed_output = item.get("role") == "assistant" or item.get("type") in (
                "function_call",
                "function_call_output",
            )
            if is_replayed_output:
                item.setdefault("id", f"synth_{i}")
                item.setdefault("status", "completed")

    if not body_json.get("instructions"):
        instructions = await build_stack_instructions(
            entry, _last_user_text_from_responses_input(input_items)
        )
        if instructions:
            body_json["instructions"] = instructions

    # mesmo clamp dinâmico do validate_body, no campo da Responses API; o
    # "input" pode ser string ou lista de itens — json.dumps cobre os dois.
    # ensure_ascii=False: mesmo motivo do estimate_prompt_tokens — o escape
    # \uXXXX inflaria texto acentuado ~2x
    tools = body_json.get("tools")
    extra_texts = [
        json.dumps(body_json.get("input") or "", ensure_ascii=False),
        body_json.get("instructions") or "",
    ]
    heuristic_est = estimate_prompt_tokens(tools=tools, extra_texts=extra_texts)
    exact_text = prompt_text_for_tokenize(tools=tools, extra_texts=extra_texts)
    est_tokens = await resolve_est_tokens(machine, heuristic_est, exact_text)
    apply_context_budget(body_json, machine, field="max_output_tokens", est_tokens=est_tokens)
    return body_json


THINK_CLOSE = "</think>"

# planos cujo modelo padrão roda com "thinking" ligado — o vLLM sobe sem
# --reasoning-parser (ver docker/entrypoint.sh, bug conhecido dessa combinação
# com Qwen3.5/3.6), então o raciocínio inteiro vaza pro campo "content" que o
# cliente exibe. Filtrado aqui porque o produto é BYOE: nenhuma ferramenta
# cliente (Cursor, Continue etc.) sabe separar isso sozinha.
# Pro (Qwen3.6-27B) validado em 17/07/2026: 14/15 respostas fecham com
# </think> (a exceção foi truncada por length — coberta pelo fallback do
# filtro, que devolve o buffer acumulado no fim do stream).
REASONING_LEAK_PLANS = {"VibeCoder", "Pro"}

# planos cujo pod é COMPARTILHADO entre várias stacks/tenants (ver
# check_concurrency) — hoje coincide em membros com REASONING_LEAK_PLANS, mas
# são eixos diferentes (parser de reasoning vs. topologia do pod) que podem
# divergir; não reaproveitar um pelo outro.
SHARED_POD_PLANS = {"VibeCoder", "Pro"}


def split_reasoning(text: str) -> tuple[str | None, str]:
    """Separa o bloco de raciocínio da resposta final. Modelos com thinking
    ligado não emitem a tag de abertura no texto gerado (o chat template já
    injeta "<think>\\n" no prompt) — só a de fechamento. Sem </think> na
    string, devolve (None, texto original): não há nada pra filtrar."""
    idx = text.find(THINK_CLOSE)
    if idx == -1:
        return None, text
    return text[:idx], text[idx + len(THINK_CLOSE) :].lstrip("\n")


async def filtered_reasoning_stream(upstream: httpx.Response, flight_key: tuple):
    """Envolve o stream SSE bruto do vLLM suprimindo os chunks de raciocínio
    (antes de </think>) e só repassando ao cliente o que vem depois. Se o
    teto de tokens for atingido sem nunca fechar </think> (raro, ~0-5% mesmo
    com o piso de max_tokens), devolve o raciocínio acumulado no chunk final
    em vez de descartar a resposta em silêncio."""
    buffer_text = ""
    in_reasoning = True
    pending = b""
    try:
        try:
            async for raw in upstream.aiter_bytes():
                pending += raw
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    stripped = line.strip()
                    if not in_reasoning or not stripped.startswith(b"data:") or stripped in (
                        b"data: [DONE]",
                        b"data:[DONE]",
                    ):
                        yield line + b"\n"
                        continue

                    payload = stripped[len(b"data:") :].strip()
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        yield line + b"\n"
                        continue

                    choices = chunk.get("choices") or []
                    choice0 = choices[0] if choices and isinstance(choices[0], dict) else None
                    if choice0 is None:
                        yield line + b"\n"
                        continue

                    delta = choice0.get("delta") or {}

                    # vLLM com --reasoning-parser (ENABLE_TOOL_CALLING, ver
                    # entrypoint.sh) já separa o raciocínio em
                    # "reasoning_content" — nesse caso "content" nunca vem
                    # com <think>, e o buffer abaixo nunca veria um </think>
                    # pra fechar, represando a resposta INTEIRA até o fim do
                    # stream (todo o texto sairia de uma vez só no fallback,
                    # quebrando streaming incremental). Detectar isso aqui e
                    # desligar o filtro nesta resposta evita esse represamento.
                    if "reasoning_content" in delta:
                        in_reasoning = False
                        if buffer_text:
                            flushed_delta = dict(delta)
                            flushed_delta["content"] = buffer_text
                            flushed_choice = dict(choice0)
                            flushed_choice["delta"] = flushed_delta
                            flushed_chunk = dict(chunk)
                            flushed_chunk["choices"] = [flushed_choice]
                            yield b"data: " + json.dumps(flushed_chunk).encode() + b"\n"
                            buffer_text = ""
                        yield line + b"\n"
                        continue

                    content = delta.get("content")
                    finish_reason = choice0.get("finish_reason")
                    if content:
                        buffer_text += content

                    if THINK_CLOSE in buffer_text:
                        _, visible = split_reasoning(buffer_text)
                        in_reasoning = False
                        buffer_text = ""
                        if visible or finish_reason:
                            delta = dict(delta)
                            delta["content"] = visible
                            delta.setdefault("role", "assistant")
                            choice0["delta"] = delta
                            yield b"data: " + json.dumps(chunk).encode() + b"\n"
                        continue

                    if finish_reason:
                        # bateu finish_reason sem nunca ver </think> — devolve o
                        # que foi acumulado em vez de sumir com a resposta inteira
                        delta = dict(delta)
                        delta["content"] = buffer_text
                        delta.setdefault("role", "assistant")
                        choice0["delta"] = delta
                        buffer_text = ""
                        in_reasoning = False
                        yield b"data: " + json.dumps(chunk).encode() + b"\n"
                        continue

                    continue  # ainda dentro do raciocínio, sem finish_reason -> suprime
            if pending:
                yield pending
        except (httpx.HTTPError, ConnectionError, OSError):
            # a conexão com o upstream (agent/vLLM) morreu no meio do stream —
            # visto sob concorrência pesada (conexão do pool resetada pelo
            # Cloudflare enquanto ociosa). Não deixa a exceção estourar em
            # silêncio: cai no fallback abaixo, que devolve o que já foi
            # acumulado em vez de fechar a resposta sem nada.
            pass
        if in_reasoning and buffer_text:
            # a conexão upstream acabou sem nunca fechar </think> nem mandar um
            # finish_reason (visto sob concorrência pesada — provável preempção/
            # aborto do vLLM, não um bug de framing) — melhor devolver o que foi
            # acumulado do que deixar o cliente sem nenhuma resposta
            fallback = {
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": buffer_text}, "finish_reason": "stop"}
                ],
            }
            yield b"data: " + json.dumps(fallback).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"
    finally:
        await upstream.aclose()
        release_flight(flight_key)


# ---------- Claude Code (Anthropic Messages API) ----------
#
# Registrados ANTES do catch-all /v1/{path:path} abaixo: o Starlette casa
# rotas na ordem de declaração, e o catch-all engoliria /v1/messages* se
# viesse primeiro. O Claude Code só fala esse formato (não tem suporte a
# backend OpenAI-compatível) — ver anthropic_compat.py pro porquê e os
# limites da tradução.


@app.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="corpo da requisição excede o limite")

    entry, key_hash, bearer_header = await authenticate_anthropic(authorization, x_api_key)
    check_rate_limit(key_hash)
    account_id = entry["account_id"]
    _, key_plan = resolve_key_stack(entry)
    await check_token_quota(account_id, key_plan)

    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    try:
        anthropic_body = json.loads(raw_body)
    except Exception:
        release_flight(flight_key)
        raise HTTPException(status_code=400, detail="corpo inválido")

    openai_body, requested_model = anthropic_to_openai_request(anthropic_body)
    is_stream = bool(anthropic_body.get("stream"))
    # mesmo filtro de <think> do chat/completions (main.py:REASONING_LEAK_PLANS)
    # — sem isso, Claude Code apontado pra um plano sem reasoning-parser
    # (ver ENABLE_TOOL_CALLING) recebia o raciocínio cru misturado no texto
    filter_reasoning = effective_plan in REASONING_LEAK_PLANS

    try:
        # mesmo validate_body do chat/completions: trava o model, aplica
        # piso/teto de max_tokens e clamp de parâmetros. O "system"
        # convertido acima já entra como client_systems (é o system prompt
        # do próprio Claude Code) — respeitado, sem injetar o da stack por
        # cima (mesma política de todos os outros canais)
        openai_body = await validate_body(openai_body, entry, rewrite_model, machine, stack_id)
    except HTTPException:
        release_flight(flight_key)
        raise

    upstream_body = json.dumps(openai_body).encode()

    try:
        upstream_req = proxy_client.build_request(
            "POST",
            f"{machine['public_url']}/v1/chat/completions",
            content=upstream_body,
            headers={"Authorization": bearer_header, "Content-Type": "application/json"},
        )
        upstream = await proxy_client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        release_flight(flight_key)
        logger.warning("anthropic proxy: upstream indisponível para %s (%s)", flight_key, e)
        raise HTTPException(status_code=503, detail="máquina indisponível, tente novamente")
    except BaseException:
        release_flight(flight_key)
        raise

    if is_stream:
        if upstream.status_code >= 400:
            # o upstream (vLLM) recusou a request antes de gerar qualquer chunk
            # SSE — o corpo é um erro OpenAI/FastAPI comum (JSON, não SSE). Se
            # deixarmos isso cair no anthropic_sse_from_openai_stream, o loop
            # de parsing (que só entende linhas "data: ...") descarta o corpo
            # inteiro e emite um stream vazio "bem-sucedido" (content: [],
            # stop_reason: end_turn) com status_code=400 por cima — o Claude
            # Code mostra "API Error: 400" seguido do stream vazio, sem
            # nenhuma pista da causa real. Aqui devolvemos o erro de verdade,
            # no formato que a Anthropic Messages API usa.
            error_raw = await upstream.aread()
            await upstream.aclose()
            release_flight(flight_key)
            logger.warning(
                "anthropic proxy: upstream %s retornou %s para %s: %s",
                machine["id"], upstream.status_code, flight_key, error_raw[:500],
            )
            try:
                error_detail = json.loads(error_raw)
                message = (
                    error_detail.get("message")
                    or (error_detail.get("error") or {}).get("message")
                    or error_detail.get("detail")
                    or error_raw.decode(errors="replace")
                )
            except Exception:
                message = error_raw.decode(errors="replace") or "erro desconhecido do modelo"
            return JSONResponse(
                status_code=upstream.status_code,
                content=anthropic_error_body(message),
            )
        return StreamingResponse(
            anthropic_sse_from_openai_stream(
                upstream, requested_model,
                on_done=lambda: release_flight(flight_key),
                filter_reasoning=filter_reasoning,
            ),
            status_code=upstream.status_code,
            media_type="text/event-stream",
        )

    try:
        raw = await upstream.aread()
        try:
            openai_resp = json.loads(raw)
            if filter_reasoning:
                for choice in openai_resp.get("choices", []):
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        reasoning, visible = split_reasoning(message["content"])
                        if reasoning is not None:
                            message["content"] = visible
            anthropic_resp = openai_to_anthropic_response(openai_resp, requested_model)
            raw = json.dumps(anthropic_resp).encode()
        except Exception:
            pass  # resposta não é o JSON esperado -> repassa como veio
    finally:
        await upstream.aclose()
        release_flight(flight_key)
    return Response(content=raw, status_code=upstream.status_code, media_type="application/json")


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    """Estimativa heurística (~4 chars/token), não a contagem exata do
    tokenizer do modelo servido — o Claude Code usa isso pra gerenciar a
    janela de contexto, não pra billing, então a aproximação é aceitável.
    Não passa por rate limit/quota/concorrência: não há inferência aqui,
    só autenticação (mesma chave) e um cálculo local barato."""
    await authenticate_anthropic(authorization, x_api_key)
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="corpo da requisição excede o limite")
    try:
        anthropic_body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="corpo inválido")
    openai_body, _ = anthropic_to_openai_request(anthropic_body)
    # mesma heurística do clamp de janela (context_budget) — inclui as tools,
    # que nos clientes agênticos são a maior fatia do prompt; sem elas o
    # Claude Code subestimava o uso e compactava tarde demais
    estimated = estimate_prompt_tokens(
        messages=openai_body.get("messages"), tools=openai_body.get("tools")
    )
    return {"input_tokens": estimated}


# paths do vLLM que o gateway repassa — qualquer coisa fora daqui (ex.:
# load_lora_adapter, unload_lora_adapter, tokenize) nunca chega nem a
# autenticar. Antes desta allowlist, um usuário comum autenticado tinha
# acesso irrestrito a QUALQUER endpoint /v1/* do vLLM, incluindo os
# administrativos (ver furo B do plano — cross-tenant via load/unload de
# adapter alheio). "models" é permitido mas tratado à parte (filtra acct-*).
ALLOWED_V1: dict[str, set[str]] = {
    "chat/completions": {"POST"},
    "completions": {"POST"},
    "embeddings": {"POST"},
    "models": {"GET"},
    "responses": {"POST"},  # Codex CLI (0.59+) só fala essa API, não chat/completions
}


def release_flight(flight_key: tuple[str, str]) -> None:
    in_flight[flight_key] -= 1


def machine_capacity(machine: dict) -> int:
    """Teto de sequências concorrentes do pod (espelha o --max-num-seqs real
    do deploy, machines.max_concurrent_seqs — migration 0028). Sem valor
    configurado ainda, cai no fallback global conservador em vez de travar
    o request."""
    cap = machine.get("max_concurrent_seqs")
    return cap if isinstance(cap, int) and cap > 0 else DEFAULT_MAX_CONCURRENT_SEQS


def check_concurrency(flight_key: tuple[str, str], machine: dict, plan: str) -> None:
    """Concorrência elástica por MÁQUINA, não por chave: uma stack sozinha no
    pod pode ocupar quase toda a capacidade; outras dividem o mesmo teto
    conforme aparecem. Em pod compartilhado (SHARED_POD_PLANS) reserva um
    piso mínimo (MIN_RESERVED_SLOTS_SHARED_POD) pra quem chegar depois nunca
    ficar 100% bloqueado esperando um tenant pesado — em pod dedicado não há
    vizinho pra proteger, o teto é a capacidade cheia.

    Chamada logo após incrementar in_flight[flight_key] (mesmo padrão do
    antigo teto por chave): se estourar, desfaz o incremento e rejeita."""
    machine_id = flight_key[1]
    reserved = MIN_RESERVED_SLOTS_SHARED_POD if plan in SHARED_POD_PLANS else 0
    ceiling = max(machine_capacity(machine) - reserved, 1)
    total_on_machine = sum(n for (_, m), n in in_flight.items() if m == machine_id)
    if total_on_machine > ceiling:
        release_flight(flight_key)
        raise HTTPException(
            status_code=429,
            detail="máquina no limite de capacidade concorrente no momento, tente novamente",
            headers={"Retry-After": "2"},
        )


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, authorization: str | None = Header(None)):
    allowed_methods = ALLOWED_V1.get(path)
    if not allowed_methods or request.method not in allowed_methods:
        raise HTTPException(status_code=404, detail="not found")

    # lido cedo (antes de autenticar/resolver rota) pra rejeitar corpo grande
    # sem pagar o custo de wake/provisionamento numa request que será recusada
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="corpo da requisição excede o limite")

    entry, key_hash = await authenticate(authorization)
    check_rate_limit(key_hash)
    account_id = entry["account_id"]
    _, key_plan = resolve_key_stack(entry)
    await check_token_quota(account_id, key_plan)

    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    # incrementa ANTES dos awaits lentos (leitura do body, embeddings do RAG):
    # o grace recheck da auto-pausa conta este in_flight — quanto mais cedo,
    # menor a janela pra pausa derrubar a máquina com request já resolvido
    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    body_json = None
    try:
        if body:
            try:
                body_json = json.loads(body)
                # validate_body (chat/completions/embeddings) ou
                # validate_responses_body (Codex, formato Responses) travam
                # o model, aplicam piso/teto de tokens e clamp de parâmetros,
                # e injetam system prompt da stack + RAG no formato certo
                if path == "responses":
                    body_json = await validate_responses_body(
                        body_json, entry, rewrite_model, machine, stack_id
                    )
                else:
                    body_json = await validate_body(
                        body_json, entry, rewrite_model, machine, stack_id
                    )
                body = json.dumps(body_json).encode()
            except HTTPException:
                raise  # rejeição explícita (ex.: limite de mensagens) não pode virar "segue como está"
            except Exception:
                pass  # body não-JSON segue como está

        upstream_req = proxy_client.build_request(
            request.method,
            f"{machine['public_url']}/v1/{path}",
            content=body,
            headers={
                # repassa a Bearer original: o agent valida e conta uso por chave
                "Authorization": authorization,
                "Content-Type": request.headers.get("content-type", "application/json"),
            },
        )
        upstream = await proxy_client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        release_flight(flight_key)
        # detalhe da exceção (pode conter a public_url interna do pod) só no
        # log do servidor — o cliente recebe uma mensagem genérica
        logger.warning("proxy: upstream indisponível para %s (%s)", flight_key, e)
        raise HTTPException(status_code=503, detail="máquina indisponível, tente novamente")
    except BaseException:
        # cliente desconectou no meio do body (CancelledError) ou qualquer
        # outra falha — nunca vazar o contador, senão a máquina fica com
        # in_flight > 0 pra sempre e a auto-pausa nunca mais dispara
        release_flight(flight_key)
        raise

    if path == "models":
        # lista também os adapters LoRA carregados na máquina ("acct-<uuid>")
        # — sem filtrar, qualquer tenant autenticado enumerava os account_id
        # de TODOS os outros tenants que dividem o mesmo pod
        try:
            raw = await upstream.aread()
            try:
                payload = json.loads(raw)
                payload["data"] = [
                    m for m in payload.get("data", [])
                    if not str(m.get("id", "")).startswith("acct-")
                ]
                raw = json.dumps(payload).encode()
            except Exception:
                pass
        finally:
            await upstream.aclose()
            release_flight(flight_key)
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    filter_reasoning = path == "chat/completions" and effective_plan in REASONING_LEAK_PLANS
    is_stream_request = isinstance(body_json, dict) and body_json.get("stream") is True

    if filter_reasoning and not is_stream_request:
        try:
            raw = await upstream.aread()
            try:
                payload = json.loads(raw)
                for choice in payload.get("choices", []):
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        reasoning, visible = split_reasoning(message["content"])
                        if reasoning is not None:
                            message["content"] = visible
                raw = json.dumps(payload).encode()
            except Exception:
                pass  # resposta não é o JSON de chat completion esperado -> repassa como veio
        finally:
            await upstream.aclose()
            release_flight(flight_key)
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    if filter_reasoning:
        return StreamingResponse(
            filtered_reasoning_stream(upstream, flight_key),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    async def stream_and_release():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            release_flight(flight_key)

    return StreamingResponse(
        stream_and_release(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


# ---------- Health e admin ----------


@app.get("/")
async def root():
    return {"ok": True, "service": "gateway"}


@app.get("/health")
async def health():
    return {"ok": True, "uptime_s": time.time() - STARTED_AT}


@app.get("/admin/routes")
async def admin_routes(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    in_flight_by_machine: dict[str, int] = defaultdict(int)
    for (_, m), n in in_flight.items():
        in_flight_by_machine[m] += n
    return {
        "in_flight": {f"{a}@{m}": n for (a, m), n in in_flight.items() if n > 0},
        # agregado por máquina — o número que importa pra ver o teto elástico
        # de check_concurrency em ação (compara com machines.max_concurrent_seqs)
        "in_flight_by_machine": {m: n for m, n in in_flight_by_machine.items() if n > 0},
        "key_cache_size": len(key_cache),
        "provisioning_in_progress": sorted(provisioning_in_progress),
    }


@app.post("/admin/flush-key-cache")
async def flush_key_cache(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    n = len(key_cache)
    key_cache.clear()
    return {"ok": True, "flushed": n}


@app.post("/admin/sync-machine-keys")
async def admin_sync_machine_keys(request: Request, x_admin_secret: str | None = Header(None)):
    """Agenda o reenvio das chaves da máquina quando o pod ficar saudável.
    Chamado pelo painel após o startMachine (o pod religa com o agent zerado
    e o poll de saúde precisa viver num processo longo — este aqui, não numa
    função serverless)."""
    require_admin(x_admin_secret)
    body = await request.json()
    machine_id = body.get("machine_id")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id é obrigatório")
    _forget_machine_upserts(machine_id)
    schedule_key_sync(machine_id)
    return {"ok": True, "scheduled": True}


@app.post("/admin/migrate")
async def admin_migrate(request: Request, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    body = await request.json()
    stack_id = body.get("stack_id")
    target = body.get("target_machine_id")
    if not stack_id or not target:
        raise HTTPException(status_code=400, detail="stack_id e target_machine_id são obrigatórios")
    try:
        return await lifecycle_mgr.migrate(stack_id, target)
    except MigrationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/admin/reap-idle")
async def admin_reap_idle(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo do idle reaper manualmente (útil em teste)."""
    require_admin(x_admin_secret)
    reaped = await lifecycle_mgr.reap_idle_once()
    return {"ok": True, "reaped": reaped}


@app.post("/admin/consolidate")
async def admin_consolidate(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de consolidação manualmente (útil em teste)."""
    require_admin(x_admin_secret)
    moved = await lifecycle_mgr.consolidate_once()
    return {"ok": True, "moved": moved}


@app.post("/admin/stop-idle-machines")
async def admin_stop_idle_machines(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de auto-pausa manualmente (útil em teste)."""
    require_admin(x_admin_secret)
    stopped = await lifecycle_mgr.stop_idle_machines_once()
    return {"ok": True, "stopped": stopped}


@app.post("/admin/ensure-capacity")
async def admin_ensure_capacity(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de reposição proativa manualmente (útil em teste e
    chamado pelo painel na hora em que o interruptor liga, pra não esperar
    até 5min pelo próximo tick automático)."""
    require_admin(x_admin_secret)
    triggered = await lifecycle_mgr.ensure_capacity_once()
    return {"ok": True, "triggered_plans": triggered}
