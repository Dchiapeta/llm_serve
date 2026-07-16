"""
Gateway estável de inferência — o ÚNICO endpoint público que o cliente final
conhece. Resolve em qual máquina está o adapter LoRA da conta e faz o proxy
(incluindo streaming SSE) para o agent daquele pod. O cliente nunca sabe em
qual pod está.

Fluxo por request:
  1. Autentica a chave HEX (Bearer) contra api_keys no Supabase (cache TTL),
     junto com o plano e o system_prompt configurado da conta.
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
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from lifecycle import LifecycleManager, MigrationError
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

# último touch por conta e por máquina (throttle)
last_touch: dict[str, float] = {}
last_machine_touch: dict[str, float] = {}

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

logger = logging.getLogger("gateway")

lifecycle_mgr: "LifecycleManager"
runpod_client: RunPodClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global supa, store, proxy_client, openai_client, panel_client, lifecycle_mgr, runpod_client
    supa = SupaClient(SUPABASE_URL, SERVICE_ROLE_KEY, LORA_BUCKET)
    store = RoutingStore(SUPABASE_URL, SERVICE_ROLE_KEY)
    proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=5.0)
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
    )
    reaper_task = asyncio.create_task(lifecycle_mgr.idle_reaper_loop())
    machine_task = asyncio.create_task(
        lifecycle_mgr.machine_lifecycle_loop(CONSOLIDATION_INTERVAL_S)
    )
    yield
    reaper_task.cancel()
    machine_task.cancel()
    await proxy_client.aclose()
    await openai_client.aclose()
    await panel_client.aclose()
    await store.aclose()
    await supa.aclose()
    if runpod_client:
        await runpod_client.aclose()


app = FastAPI(lifespan=lifespan)


def require_admin(secret: str | None):
    if not GATEWAY_ADMIN_SECRET or secret != GATEWAY_ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="admin secret inválido")


def lora_name(account_id: str) -> str:
    return f"acct-{account_id}"


# ---------- Autenticação ----------


async def authenticate(authorization: str | None) -> dict:
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
    return entry


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
        "key_prefix": entry["key_prefix"],
        "account_name": entry["account_name"],
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


async def wake_machine(machine: dict, reason: str) -> bool:
    """Religa um pod pausado (startPod) e o devolve ao pool de roteamento.

    O touch de atividade vem ANTES do flip para running: sem ele, o
    last_activity_at velho faria a auto-pausa parar a máquina de novo no
    próximo ciclo, enquanto o vLLM ainda carrega o modelo."""
    if runpod_client is None or not machine.get("runpod_pod_id"):
        return False
    now = time.time()
    if now - last_wake_attempt.get(machine["id"], 0) < WAKE_COOLDOWN_S:
        return False
    # marca a tentativa antes do primeiro await — atômico dentro do event loop
    last_wake_attempt[machine["id"]] = now
    try:
        await runpod_client.start_pod(machine["runpod_pod_id"])
    except Exception as e:
        # ex.: "not enough free GPUs" — host cedeu a GPU; o chamador tenta outra
        logger.warning("auto-wake: startPod de %s falhou (%s)", machine["id"], e)
        return False
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
    return True


async def wake_some_machine_for_plan(plan: str) -> bool:
    """Religa a primeira máquina pausada do template do plano que aceitar o
    start. Só é chamado quando não há NENHUMA máquina running com vaga."""
    for m in await supa.list_stopped_machines_for_plan(plan):
        if await wake_machine(m, "requisição recebida sem máquina disponível"):
            return True
    return False


def waking_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="máquina religando, tente novamente em alguns minutos",
        headers={"Retry-After": "60"},
    )


def provisioning_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="provisionando uma máquina nova, tente novamente em alguns minutos",
        headers={"Retry-After": str(int(PROVISION_RETRY_AFTER_S))},
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
    if await wake_some_machine_for_plan(plan):
        raise waking_503()
    if plan in provisioning_in_progress or await try_provision_for_request(
        plan, "sem máquina com vaga nem pausada"
    ):
        raise provisioning_503()
    if not machines:
        raise HTTPException(status_code=503, detail="nenhuma máquina disponível")
    raise HTTPException(status_code=503, detail="sem capacidade: todas as máquinas estão cheias")


async def do_load(account_id: str, entry: dict, machine: dict, adapter: dict) -> None:
    """Garante a chave no agent, baixa+carrega o adapter e confirma a rota."""
    await ensure_key_on_machine(entry, machine)
    files = await supa.signed_lora_files(adapter["storage_path"])
    await call_agent(
        machine, "/load-lora",
        {"lora_name": lora_name(account_id), "files": files},
        timeout_s=LORA_LOAD_TIMEOUT_S,
    )
    await store.set_client_location(
        account_id,
        machine_id=machine["id"],
        lora_adapter_id=adapter["id"],
        lora_status="loaded",
    )


async def wait_until_routed(account_id: str) -> dict | None:
    """Espera (poll curto) um load em andamento de outro request terminar."""
    deadline = time.time() + LOAD_WAIT_TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        route = await store.get_client_location(account_id)
        if route and route["lora_status"] in ("loaded", "migrating") and route["machine_id"]:
            return route
        if not route or route["lora_status"] == "unloaded":
            return None  # o load falhou e o slot foi liberado
    return None


def pick_stack(entry: dict) -> dict | None:
    """Stack "casa" da conta pro modelo base: a mais recente do plano da
    conta; sem stack do plano, a mais recente de qualquer plano; None quando
    a conta não tem stacks (aí o fallback é o roteamento por plano puro)."""
    stacks = entry.get("stacks") or []
    if not stacks:
        return None
    same_plan = [s for s in stacks if s.get("plan") == entry["plan"]]
    pool = same_plan or stacks
    return max(pool, key=lambda s: s.get("created_at") or "")


async def pick_running_machine_with_stack_slot(
    plan: str, exclude_machine_id: str | None = None
) -> dict | None:
    """Primeira máquina running do plano com vaga de stack ("1 stack = 1
    slot"). Capacidade desconhecida/sem teto (slots 0) é aceita — mesmo
    critério do allocateMachineForTemplate do painel (lib/actions.ts)."""
    for m in await supa.list_running_machines_for_plan(plan):
        if exclude_machine_id and m["id"] == exclude_machine_id:
            continue
        slots = await supa.machine_stack_slots(m["id"])
        if slots is None or slots == 0:
            return m
        if slots - await supa.count_stacks_on_machine(m["id"]) > 0:
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
    async with realloc_locks[entry["plan"]]:
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
                entry["plan"], exclude_machine_id=old_machine["id"]
            )
            if not target:
                return None
            if not await supa.repoint_stack(stack["id"], old_machine["id"], target["id"]):
                return None
            await supa.move_account_keys(
                entry["account_id"], old_machine["id"], target["id"]
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


async def resolve_base_machine(account_id: str, entry: dict) -> dict:
    """Máquina pro modelo base (conta sem adapter), stack-aware.

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
    stack = pick_stack(entry)
    if stack and stack.get("machine_id"):
        machine = await supa.get_machine(stack["machine_id"])
        if machine:
            status = machine.get("status")
            if status == "running" and machine.get("public_url"):
                await ensure_key_on_machine(entry, machine)
                return machine
            if status in ("stopped", "terminated", "error"):
                target = await reallocate_stack(entry, stack, machine)
                if target:
                    return target
                if status == "stopped":
                    slug = stack.get("slug") or stack["id"]
                    if await wake_machine(
                        machine, f"stack {slug}: máquina pausada e sem vaga nas demais"
                    ):
                        raise waking_503()
                    fresh = await supa.get_machine(machine["id"])
                    if fresh and fresh.get("status") == "running":
                        # request concorrente religou primeiro (o cooldown fez
                        # o wake_machine devolver False) — o pod ainda está
                        # subindo, não religa uma 2ª máquina à toa
                        raise waking_503()
                    # startPod falhou de verdade (ex.: host sem GPU) → segue
                    # pro fallback por plano: serve temporário sem mover stack

    machines = await supa.list_running_machines_for_plan(entry["plan"])
    if not machines:
        if await wake_some_machine_for_plan(entry["plan"]):
            raise waking_503()
        plan = entry["plan"]
        if plan in provisioning_in_progress or await try_provision_for_request(
            plan, "sem máquina para o modelo base"
        ):
            raise provisioning_503()
        raise HTTPException(
            status_code=503, detail=f"nenhuma máquina disponível para o plano {entry['plan']}"
        )
    machine = machines[0]
    await ensure_key_on_machine(entry, machine)
    return machine


async def resolve_route(account_id: str, entry: dict) -> tuple[dict, bool]:
    """Resolve (machine, rewrite_model) para a conta.

    Regra primária: rota com machine_id e status loaded/migrating → proxy
    direto (migrating = origem continua servindo). Sem adapter registrado →
    modelo base numa máquina running, sem reescrever "model".
    """
    route = await store.get_client_location(account_id)

    if route and route["machine_id"] and route["lora_status"] in ("loaded", "migrating"):
        machine = await supa.get_machine(route["machine_id"])
        if not machine:
            raise HTTPException(status_code=503, detail="máquina da rota não existe mais")
        if machine.get("status") == "stopped":
            # stop manual pelo painel com a rota ainda apontando pra máquina
            # (a auto-pausa exige 0 rotas, então nunca cria este estado). O
            # restart zera a VRAM — o adapter não está mais carregado mesmo —
            # então libera o slot e cai no fluxo sem-rota: realoca em máquina
            # running com vaga ou dispara o auto-wake.
            await store.mark_slot_idle(account_id)
            route = None
        else:
            return machine, True

    if route and route["lora_status"] == "loading":
        waited = await wait_until_routed(account_id)
        if waited:
            machine = await supa.get_machine(waited["machine_id"])
            if machine:
                return machine, True
        raise HTTPException(
            status_code=503,
            detail="adapter carregando, tente novamente",
            headers={"Retry-After": "5"},
        )

    # sem rota ativa: conta tem adapter?
    adapter = await supa.latest_ready_adapter(account_id)
    if not adapter:
        # sem adapter registrado → serve o modelo base, stack-aware: máquina
        # do stack da conta quando running; pausada → realocação automática
        # ou wake da própria; fallback por plano (VibeCoder nunca cai numa
        # máquina servindo o modelo do Pro/Max, e vice-versa)
        return await resolve_base_machine(account_id, entry), False

    machine = await pick_machine_with_free_slot(entry["plan"])
    result = await store.claim_client_location(account_id, machine["id"])
    if not result["claimed"]:
        # outro request venceu a corrida — espera o load dele
        waited = await wait_until_routed(account_id)
        if waited:
            m = await supa.get_machine(waited["machine_id"])
            if m:
                return m, True
        raise HTTPException(
            status_code=503,
            detail="adapter carregando, tente novamente",
            headers={"Retry-After": "5"},
        )

    try:
        await do_load(account_id, entry, machine, adapter)
    except HTTPException:
        await store.mark_slot_idle(account_id)
        raise
    except Exception as e:
        await store.mark_slot_idle(account_id)
        raise HTTPException(status_code=503, detail=f"falha ao carregar adapter: {e}")
    return machine, True


# ---------- Proxy ----------


async def maybe_touch(account_id: str, machine_id: str | None = None):
    now = time.time()
    if now - last_touch.get(account_id, 0) >= TOUCH_THROTTLE_S:
        last_touch[account_id] = now
        try:
            await store.touch(account_id)
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


async def augment_body(body_json: dict, entry: dict) -> dict:
    """Injeta system prompt configurado da conta e contexto de RAG (top-k da
    base de conhecimento) antes de repassar ao vLLM. Só se aplica a chamadas
    de chat completions (body com "messages")."""
    messages = body_json.get("messages")
    if not isinstance(messages, list):
        return body_json

    if entry.get("system_prompt"):
        messages.insert(0, {"role": "system", "content": entry["system_prompt"]})

    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"), None
    )
    if last_user and isinstance(last_user.get("content"), str):
        embedding = await embed_query(last_user["content"])
        if embedding:
            chunks = await supa.match_knowledge_chunks(
                entry["account_id"], embedding, RAG_TOP_K
            )
            if chunks:
                context_msg = {
                    "role": "system",
                    "content": "Contexto relevante da base de conhecimento:\n"
                    + "\n---\n".join(chunks),
                }
                messages.insert(messages.index(last_user), context_msg)

    body_json["messages"] = messages
    return body_json


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, authorization: str | None = Header(None)):
    entry = await authenticate(authorization)
    account_id = entry["account_id"]

    machine, rewrite_model = await resolve_route(account_id, entry)
    await maybe_touch(account_id, machine["id"])

    # incrementa ANTES dos awaits lentos (leitura do body, embeddings do RAG):
    # o grace recheck da auto-pausa conta este in_flight — quanto mais cedo,
    # menor a janela pra pausa derrubar a máquina com request já resolvido
    flight_key = (account_id, machine["id"])
    in_flight[flight_key] += 1

    try:
        body = await request.body()
        if body:
            try:
                body_json = json.loads(body)
                if rewrite_model and "model" in body_json:
                    body_json["model"] = lora_name(account_id)
                # augment_body é no-op se a conta não tem system_prompt nem
                # chunks indexados — sempre tentar é mais simples do que checar
                # plano aqui (a mesma injeção serve VibeCoder/Pro/Max igual)
                body_json = await augment_body(body_json, entry)
                body = json.dumps(body_json).encode()
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
        in_flight[flight_key] -= 1
        raise HTTPException(status_code=503, detail=f"máquina indisponível: {e}")
    except BaseException:
        # cliente desconectou no meio do body (CancelledError) ou qualquer
        # outra falha — nunca vazar o contador, senão a máquina fica com
        # in_flight > 0 pra sempre e a auto-pausa nunca mais dispara
        in_flight[flight_key] -= 1
        raise

    async def stream_and_release():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            in_flight[flight_key] -= 1

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
    return {
        "in_flight": {f"{a}@{m}": n for (a, m), n in in_flight.items() if n > 0},
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
    account_id = body.get("account_id")
    target = body.get("target_machine_id")
    if not account_id or not target:
        raise HTTPException(status_code=400, detail="account_id e target_machine_id são obrigatórios")
    try:
        return await lifecycle_mgr.migrate(account_id, target)
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
