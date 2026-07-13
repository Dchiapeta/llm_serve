"""
Gateway estável de inferência — o ÚNICO endpoint público que o cliente final
conhece. Resolve em qual máquina está o adapter LoRA da conta e faz o proxy
(incluindo streaming SSE) para o agent daquele pod. O cliente nunca sabe em
qual pod está.

Fluxo por request:
  1. Autentica a chave HEX (Bearer) contra api_keys no Supabase (cache TTL).
  2. Resolve a rota (routing_state). Regra primária: machine_id definido →
     proxy direto, independente do status (em 'migrating' a origem segue
     servindo até o flip). Só espera quando não há máquina servindo.
  3. Sem rota: alocação placeholder (primeira máquina running com slot livre),
     claim atômico, upsert da chave no agent, load do adapter, proxy.
  4. Máquina fora do ar → 503 imediato, nunca pendura o request.

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
    global supa, store, proxy_client, lifecycle_mgr
    supa = SupaClient(SUPABASE_URL, SERVICE_ROLE_KEY, LORA_BUCKET)
    store = RoutingStore(SUPABASE_URL, SERVICE_ROLE_KEY)
    proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=5.0)
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


async def pick_machine_with_free_slot() -> dict:
    """Alocação placeholder: primeira máquina running com slot LoRA livre.

    Slots por VRAM via machine_lora_slots() (mesma fórmula do painel);
    MAX_LORAS_PER_MACHINE atua como teto (espelho do --max-loras do pod) e
    como fallback quando a capacidade é desconhecida (sem VRAM/template).
    """
    machines = await supa.list_running_machines()
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
        # sem adapter registrado → serve o modelo base em qualquer máquina
        machines = await supa.list_running_machines()
        if not machines:
            raise HTTPException(status_code=503, detail="nenhuma máquina disponível")
        return machines[0], False

    machine = await pick_machine_with_free_slot()
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


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, authorization: str | None = Header(None)):
    entry = await authenticate(authorization)
    account_id = entry["account_id"]

    machine, rewrite_model = await resolve_route(account_id, entry)
    await maybe_touch(account_id)

    body = await request.body()
    if rewrite_model and body:
        try:
            body_json = json.loads(body)
            if "model" in body_json:
                body_json["model"] = lora_name(account_id)
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
