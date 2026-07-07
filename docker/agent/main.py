"""
Agent que roda dentro do pod, na frente do vLLM.

- Valida a chave HEX do usuário (Authorization: Bearer <chave>) contra os
  hashes sincronizados pelo painel.
- Repassa a requisição ao vLLM local e contabiliza uso por chave.
- Expõe /admin/* (protegido por AGENT_ADMIN_SECRET) para o painel:
  sync de chaves, métricas, logs da máquina e logs por usuário.
"""

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

VLLM_URL = os.environ.get("VLLM_URL", "http://127.0.0.1:8001")
ADMIN_SECRET = os.environ.get("AGENT_ADMIN_SECRET", "")
MODEL_NAME = os.environ.get("MODEL_NAME", "")
# teto de usuários definido pelo painel na criação do pod; 0 = sem teto.
# O enforcement real é do painel (emissão de chaves) — aqui é só informativo.
MAX_USERS = int(os.environ.get("MAX_USERS", "0") or 0)
VLLM_LOG_FILE = os.environ.get("VLLM_LOG_FILE", "/var/log/vllm.log")

STARTED_AT = time.time()

# chaves sincronizadas: hash -> {key_prefix, account_name}
keys_by_hash: dict[str, dict] = {}

# métricas por key_prefix
metrics_per_key: dict[str, dict] = {}
total_requests = 0
concurrent_now = 0
concurrent_peak = 0

# logs de requisições por usuário (buffer circular)
request_logs: deque[dict] = deque(maxlen=5000)

client = httpx.AsyncClient(base_url=VLLM_URL, timeout=httpx.Timeout(600.0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan)


def require_admin(secret: str | None):
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="admin secret inválido")


def authenticate(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="chave de acesso ausente")
    key = authorization.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    entry = keys_by_hash.get(key_hash)
    if not entry:
        raise HTTPException(status_code=401, detail="chave de acesso inválida")
    return entry


def log_line(prefix: str, account: str, msg: str):
    request_logs.append(
        {
            "ts": time.time(),
            "key_prefix": prefix,
            "line": f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{account}/{prefix}…] {msg}",
        }
    )


# ---------- Rotas admin (painel) ----------


class SyncKeysBody(BaseModel):
    keys: list[dict]


@app.post("/admin/sync-keys")
async def sync_keys(body: SyncKeysBody, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    keys_by_hash.clear()
    for k in body.keys:
        keys_by_hash[k["key_hash"]] = {
            "key_prefix": k["key_prefix"],
            "account_name": k.get("account_name", "?"),
        }
    return {"ok": True, "count": len(keys_by_hash)}


@app.get("/admin/logs")
async def get_logs(
    x_admin_secret: str | None = Header(None),
    key_prefix: str | None = Query(None),
    tail: int = Query(200, le=2000),
):
    require_admin(x_admin_secret)
    if key_prefix:
        lines = [l["line"] for l in request_logs if l["key_prefix"] == key_prefix]
        return {"lines": lines[-tail:]}
    # logs da máquina inteira: stdout do vLLM + requisições
    machine_lines: list[str] = []
    try:
        with open(VLLM_LOG_FILE, "r", errors="replace") as f:
            machine_lines = f.readlines()[-tail:]
        machine_lines = [l.rstrip("\n") for l in machine_lines]
    except FileNotFoundError:
        machine_lines = ["(log do vLLM ainda não disponível)"]
    req_lines = [l["line"] for l in list(request_logs)[-tail:]]
    return {"lines": machine_lines + ["", "--- requisições ---"] + req_lines}


@app.get("/admin/metrics")
async def get_metrics(
    x_admin_secret: str | None = Header(None),
    reset: bool = Query(False),
):
    require_admin(x_admin_secret)
    global concurrent_peak
    snapshot = {
        "per_key": {p: dict(m) for p, m in metrics_per_key.items()},
        "total_requests": total_requests,
        "concurrent_now": concurrent_now,
        "concurrent_peak": concurrent_peak,
        "uptime_s": time.time() - STARTED_AT,
    }
    # reset=true entrega o delta desde a última coleta e zera os contadores,
    # para o painel gravar janelas sem contar duplicado.
    if reset:
        metrics_per_key.clear()
        concurrent_peak = concurrent_now
    return snapshot


@app.get("/admin/health")
async def admin_health(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    return {"ok": True, "model": MODEL_NAME, "max_users": MAX_USERS}


# ---------- Health público ----------


@app.get("/")
async def root():
    # o proxy/health-check do RunPod bate na raiz; sem esta rota ele recebia 404
    # e reiniciava o pod em loop, matando o vLLM antes de carregar o modelo.
    return {"ok": True, "service": "agent"}


@app.get("/health")
async def health():
    # vLLM só responde ao /health quando o modelo terminou de carregar;
    # enquanto baixa/carrega, o painel usa isso para mostrar "Subindo".
    vllm_ready = False
    try:
        r = await client.get("/health", timeout=2.0)
        vllm_ready = r.status_code == 200
    except Exception:
        pass
    return {"ok": True, "vllm_ready": vllm_ready, "model": MODEL_NAME}


# ---------- Proxy OpenAI-compatible ----------


def track_usage(prefix: str, usage: dict | None):
    m = metrics_per_key.setdefault(
        prefix, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "last_used": None}
    )
    m["requests"] += 1
    m["last_used"] = time.time()
    if usage:
        m["tokens_in"] += usage.get("prompt_tokens", 0) or 0
        m["tokens_out"] += usage.get("completion_tokens", 0) or 0


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy_vllm(path: str, request: Request, authorization: str | None = Header(None)):
    global total_requests, concurrent_now, concurrent_peak

    entry = authenticate(authorization)
    prefix = entry["key_prefix"]
    account = entry["account_name"]

    body = await request.body()
    total_requests += 1
    concurrent_now += 1
    concurrent_peak = max(concurrent_peak, concurrent_now)
    log_line(prefix, account, f"{request.method} /v1/{path}")

    try:
        # detecta streaming para repassar SSE; injeta include_usage para o
        # vLLM emitir o chunk final com usage (senão tokens não são contados)
        is_stream = False
        try:
            body_json = json.loads(body)
            is_stream = body_json.get("stream") is True
            if is_stream:
                opts = body_json.setdefault("stream_options", {})
                opts.setdefault("include_usage", True)
                body = json.dumps(body_json).encode()
        except Exception:
            pass  # body não-JSON: segue como não-streaming

        if is_stream:
            upstream_req = client.build_request(
                request.method, f"/v1/{path}", content=body,
                headers={"Content-Type": "application/json"},
            )
            upstream = await client.send(upstream_req, stream=True)

            async def stream_and_close():
                usage = None
                buffer = b""
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                        # o chunk com usage é o último; basta guardar o final
                        buffer = (buffer + chunk)[-16384:]
                finally:
                    await upstream.aclose()
                    for line in buffer.split(b"\n"):
                        line = line.strip()
                        if line.startswith(b"data:") and b'"usage"' in line:
                            try:
                                payload = json.loads(line[5:])
                                if payload.get("usage"):
                                    usage = payload["usage"]
                            except Exception:
                                pass
                    global concurrent_now
                    concurrent_now -= 1
                    track_usage(prefix, usage)
                    log_line(
                        prefix, account,
                        f"stream concluído ({upstream.status_code}) · "
                        f"{usage.get('total_tokens', '?') if usage else '?'} tokens",
                    )

            return StreamingResponse(
                stream_and_close(),
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "text/event-stream"),
            )

        resp = await client.request(
            request.method, f"/v1/{path}", content=body,
            headers={"Content-Type": "application/json"},
        )
        concurrent_now -= 1

        usage = None
        try:
            usage = resp.json().get("usage")
        except Exception:
            pass
        track_usage(prefix, usage)
        log_line(
            prefix, account,
            f"{resp.status_code} · {usage.get('total_tokens', '?') if usage else '?'} tokens",
        )
        return JSONResponse(
            content=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text},
            status_code=resp.status_code,
        )
    except HTTPException:
        concurrent_now -= 1
        raise
    except Exception as e:
        concurrent_now -= 1
        log_line(prefix, account, f"erro: {e}")
        raise HTTPException(status_code=502, detail=f"vLLM indisponível: {e}")
