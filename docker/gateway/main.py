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
     adapter, a alocação é restrita às máquinas do template do plano da
     conta (accounts.plan) — nunca cai no modelo base de outro plano.
  3. Sem rota: alocação placeholder (primeira máquina running com slot livre),
     claim atômico, upsert da chave no agent, load do adapter, proxy.
  4. Injeta no body (chat completions): system prompt da conta + top-k de
     contexto da base de conhecimento (RAG básico do VibeCoder, embeddings
     via OpenAI).
  5. Máquina fora do ar → 503 imediato, nunca pendura o request.

Limitação aceita (MVP): réplica ÚNICA. O contador in-flight e (na Fase 5) o
idle reaper vivem em memória do processo — múltiplas réplicas cortariam
streams durante migração. Ver README.md.
"""

import asyncio
import hashlib
import json
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from lifecycle import LifecycleManager, MigrationError
from routing import RoutingStore
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
# lifecycle: unload por ociosidade (0 = desligado) e drain da migração
IDLE_UNLOAD_MINUTES = float(os.environ.get("IDLE_UNLOAD_MINUTES", "30"))
MIGRATION_DRAIN_TIMEOUT_S = float(os.environ.get("MIGRATION_DRAIN_TIMEOUT_S", "600"))

STARTED_AT = time.time()

supa: SupaClient
store: RoutingStore
# proxy para os agents: connect curto (máquina fora do ar → 503 rápido),
# read longo (streams de inferência podem durar minutos)
proxy_client: httpx.AsyncClient
# client curto pra API de embeddings da OpenAI (RAG do VibeCoder)
openai_client: httpx.AsyncClient

# cache de chaves: key_hash -> (entry | None, expira_em)
key_cache: dict[str, tuple[dict | None, float]] = {}

# requests em voo por (account_id, machine_id) — base do drain da Fase 5.
# Em memória: válido apenas com réplica única do gateway.
in_flight: dict[tuple[str, str], int] = defaultdict(int)

# último touch por conta (throttle)
last_touch: dict[str, float] = {}


lifecycle_mgr: "LifecycleManager"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global supa, store, proxy_client, openai_client, lifecycle_mgr
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
    lifecycle_mgr = LifecycleManager(
        store=store,
        supa=supa,
        call_agent=call_agent,
        in_flight=in_flight,
        idle_unload_minutes=IDLE_UNLOAD_MINUTES,
        drain_timeout_s=MIGRATION_DRAIN_TIMEOUT_S,
        lora_load_timeout_s=LORA_LOAD_TIMEOUT_S,
    )
    reaper_task = asyncio.create_task(lifecycle_mgr.idle_reaper_loop())
    yield
    reaper_task.cancel()
    await proxy_client.aclose()
    await openai_client.aclose()
    await store.aclose()
    await supa.aclose()


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


async def pick_machine_with_free_slot(plan: str) -> dict:
    """Alocação placeholder: primeira máquina running (do template do plano
    da conta) com slot LoRA livre.

    Slots por VRAM via machine_lora_slots() (mesma fórmula do painel);
    MAX_LORAS_PER_MACHINE atua como teto (espelho do --max-loras do pod) e
    como fallback quando a capacidade é desconhecida (sem VRAM/template).
    """
    machines = await supa.list_running_machines_for_plan(plan)
    if not machines:
        raise HTTPException(status_code=503, detail="nenhuma máquina disponível")
    for m in machines:
        by_vram = await supa.machine_lora_slots(m["id"])
        slots = MAX_LORAS_PER_MACHINE if by_vram is None else min(by_vram, MAX_LORAS_PER_MACHINE)
        used = await supa.count_active_routes(m["id"])
        if used < slots:
            return m
    raise HTTPException(status_code=503, detail="sem capacidade: todas as máquinas estão cheias")


async def do_load(account_id: str, entry: dict, machine: dict, adapter: dict) -> None:
    """Garante a chave no agent, baixa+carrega o adapter e confirma a rota."""
    await call_agent(machine, "/upsert-keys", {"keys": [{
        "key_hash": entry["key_hash"],
        "key_prefix": entry["key_prefix"],
        "account_name": entry["account_name"],
    }]})
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
        # sem adapter registrado → serve o modelo base, mas só numa máquina
        # do template compatível com o plano da conta (VibeCoder não pode
        # cair numa máquina servindo o modelo do Pro/Max, e vice-versa)
        machines = await supa.list_running_machines_for_plan(entry["plan"])
        if not machines:
            raise HTTPException(
                status_code=503, detail=f"nenhuma máquina disponível para o plano {entry['plan']}"
            )
        return machines[0], False

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


async def maybe_touch(account_id: str):
    now = time.time()
    if now - last_touch.get(account_id, 0) >= TOUCH_THROTTLE_S:
        last_touch[account_id] = now
        try:
            await store.touch(account_id)
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
    await maybe_touch(account_id)

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

    flight_key = (account_id, machine["id"])
    in_flight[flight_key] += 1

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
    try:
        upstream = await proxy_client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        in_flight[flight_key] -= 1
        raise HTTPException(status_code=503, detail=f"máquina indisponível: {e}")

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
    }


@app.post("/admin/flush-key-cache")
async def flush_key_cache(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    n = len(key_cache)
    key_cache.clear()
    return {"ok": True, "flushed": n}


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
