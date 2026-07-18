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
import hmac
import json
import os
import shutil
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

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
# diretório local onde adapters LoRA baixados do storage ficam antes do load
LORA_DIR = os.environ.get("LORA_DIR", "/workspace/loras")

STARTED_AT = time.time()

# chaves sincronizadas: hash -> {api_key_id, key_prefix, account_name}
keys_by_hash: dict[str, dict] = {}

# métricas por api_key_id (não por key_prefix: 8 hex chars = 32 bits, uma
# colisão entre duas chaves diferentes misturaria o uso de duas contas)
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
    if not ADMIN_SECRET or not secret or not hmac.compare_digest(secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="admin secret inválido")


def authenticate(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="chave de acesso ausente")
    key = authorization.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    entry = keys_by_hash.get(key_hash)
    if not entry:
        raise HTTPException(status_code=401, detail="chave de acesso inválida")

    # segunda camada de enforcement (a primeira é o gateway): fecha o bypass
    # de quem descobre a URL pública do pod e chama o agent direto, sem
    # passar pelo gateway — ali a chave expirada já seria barrada antes de
    # chegar aqui, mas o agent é alcançável sozinho (ver ALLOWED_V1 acima)
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expiry = None
        if expiry and expiry <= datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="chave expirada")

    return entry


def log_line(api_key_id: str | None, prefix: str, account: str, msg: str):
    request_logs.append(
        {
            "ts": time.time(),
            # api_key_id é o identificador ESTÁVEL pra filtrar (ver
            # /admin/logs); key_prefix fica só na linha de texto, pra leitura
            "api_key_id": api_key_id,
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
        # .get() em vez de indexação direta: um registro sem key_hash não
        # deve estourar KeyError no meio do loop (já rodou o clear() acima —
        # um erro aqui deixaria keys_by_hash só com os registros anteriores
        # a esse, derrubando 401 pra todo mundo que vinha depois na lista)
        key_hash = k.get("key_hash")
        if not key_hash:
            continue
        keys_by_hash[key_hash] = {
            "api_key_id": k.get("api_key_id"),
            "key_prefix": k.get("key_prefix", "?"),
            "account_name": k.get("account_name", "?"),
            "expires_at": k.get("expires_at"),
        }
    return {"ok": True, "count": len(keys_by_hash)}


@app.get("/admin/logs")
async def get_logs(
    x_admin_secret: str | None = Header(None),
    api_key_id: str | None = Query(None),
    tail: int = Query(200, le=2000),
):
    require_admin(x_admin_secret)
    if api_key_id:
        lines = [l["line"] for l in request_logs if l["api_key_id"] == api_key_id]
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


# Insere/atualiza chaves SEM limpar as existentes (diferente do sync-keys).
# Usado pelo gateway para garantir a chave do cliente na máquina alocada
# antes do primeiro proxy, sem sobrescrever o estado do painel.
@app.post("/admin/upsert-keys")
async def upsert_keys(body: SyncKeysBody, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    for k in body.keys:
        key_hash = k.get("key_hash")
        if not key_hash:
            continue
        keys_by_hash[key_hash] = {
            "api_key_id": k.get("api_key_id"),
            "key_prefix": k.get("key_prefix", "?"),
            "account_name": k.get("account_name", "?"),
            "expires_at": k.get("expires_at"),
        }
    return {"ok": True, "count": len(keys_by_hash)}


# ---------- Rotas admin: adapters LoRA ----------


class LoraFile(BaseModel):
    name: str
    url: str


# Whitelist explícita dos arquivos aceitos num adapter PEFT — mais restritivo
# que só bloquear path traversal: mesmo que o chamador mude no futuro, nada
# fora desta lista é gravado em disco.
LORA_REQUIRED_FILES = {"adapter_config.json", "adapter_model.safetensors"}
LORA_ALLOWED_FILES = LORA_REQUIRED_FILES | {
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "chat_template.jinja",
}


class LoadLoraBody(BaseModel):
    lora_name: str
    files: list[LoraFile]


class UnloadLoraBody(BaseModel):
    lora_name: str


def _lora_local_dir(lora_name: str) -> Path:
    # nome vem do painel/gateway (acct-<uuid>), mas sanitiza contra path traversal
    safe = os.path.basename(lora_name)
    if not safe or safe != lora_name or safe in (".", ".."):
        raise HTTPException(status_code=400, detail="lora_name inválido")
    return Path(LORA_DIR) / safe


# Baixa os arquivos do adapter (signed URLs geradas pelo chamador) para disco
# local e carrega no vLLM em runtime. Idempotente: adapter já carregado = ok.
@app.post("/admin/load-lora")
async def load_lora(body: LoadLoraBody, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    if not body.files:
        raise HTTPException(status_code=400, detail="lista de arquivos vazia")

    names = {f.name for f in body.files}
    missing = LORA_REQUIRED_FILES - names
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"adapter incompleto — faltam: {', '.join(sorted(missing))}",
        )
    unknown = names - LORA_ALLOWED_FILES
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"arquivos fora da whitelist PEFT: {', '.join(sorted(unknown))}",
        )

    target = _lora_local_dir(body.lora_name)
    target.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as dl:
        for f in body.files:
            fname = f.name  # já validado contra a whitelist acima
            try:
                async with dl.stream("GET", f.url) as r:
                    if r.status_code != 200:
                        raise HTTPException(
                            status_code=502,
                            detail=f"download de {fname} falhou ({r.status_code})",
                        )
                    with open(target / fname, "wb") as out:
                        async for chunk in r.aiter_bytes():
                            out.write(chunk)
            except httpx.HTTPError as e:
                raise HTTPException(status_code=502, detail=f"download de {fname} falhou: {e}")
    download_s = time.time() - t0

    t1 = time.time()
    resp = await client.post(
        "/v1/load_lora_adapter",
        json={"lora_name": body.lora_name, "lora_path": str(target)},
    )
    load_s = time.time() - t1

    if resp.status_code != 200:
        text = resp.text
        # vLLM 0.24: load duplicado → 400 "has already been loaded" — idempotência
        if "already" in text.lower():
            return {"ok": True, "lora_name": body.lora_name,
                    "download_s": round(download_s, 2), "load_s": round(load_s, 2),
                    "already_loaded": True}
        raise HTTPException(status_code=502, detail=f"vLLM load_lora_adapter falhou: {text}")

    return {"ok": True, "lora_name": body.lora_name,
            "download_s": round(download_s, 2), "load_s": round(load_s, 2)}


# Descarrega o adapter da VRAM e remove os arquivos locais. Idempotente.
@app.post("/admin/unload-lora")
async def unload_lora(body: UnloadLoraBody, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    target = _lora_local_dir(body.lora_name)

    resp = await client.post("/v1/unload_lora_adapter", json={"lora_name": body.lora_name})
    # vLLM 0.24: adapter não carregado → 404 "cannot be found" = já descarregado,
    # segue como sucesso (idempotência). Qualquer outro erro é propagado.
    if resp.status_code not in (200, 404):
        raise HTTPException(status_code=502, detail=f"vLLM unload_lora_adapter falhou: {resp.text}")

    shutil.rmtree(target, ignore_errors=True)
    return {"ok": True, "lora_name": body.lora_name}


# Lista os adapters atualmente carregados no vLLM (exclui o modelo base).
@app.get("/admin/loras")
async def list_loras(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    try:
        resp = await client.get("/v1/models", timeout=5.0)
        models = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"vLLM indisponível: {e}")
    return {"loras": [m for m in models if m != MODEL_NAME]}


# ---------- Health público ----------


@app.get("/")
async def root():
    # o proxy/health-check do RunPod bate na raiz; sem esta rota ele recebia 404
    # e reiniciava o pod em loop, matando o vLLM antes de carregar o modelo.
    return {"ok": True, "service": "agent"}


def _vllm_process_alive() -> bool:
    # O entrypoint sobe o vLLM como processo irmão; se ele morrer (ex.: OOM
    # na inicialização), o pod continua RUNNING mas nunca ficará pronto.
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"vllm.entrypoints.openai.api_server" in cmdline:
            return True
    return False


@app.get("/health")
async def health():
    # vLLM só responde ao /health quando o modelo terminou de carregar;
    # enquanto baixa/carrega, o painel usa isso para mostrar "Subindo".
    # vllm_alive=False com vllm_ready=False indica crash → painel mostra "Falha".
    vllm_ready = False
    try:
        r = await client.get("/health", timeout=2.0)
        vllm_ready = r.status_code == 200
    except Exception:
        pass
    vllm_alive = vllm_ready or _vllm_process_alive()
    return {
        "ok": True,
        "vllm_ready": vllm_ready,
        "vllm_alive": vllm_alive,
        "model": MODEL_NAME,
    }


# ---------- Proxy OpenAI-compatible ----------


def track_usage(api_key_id: str | None, usage: dict | None):
    if not api_key_id:
        return  # chave sem id sincronizado (sync antigo) — nada seguro pra agregar
    m = metrics_per_key.setdefault(
        api_key_id, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "last_used": None}
    )
    m["requests"] += 1
    m["last_used"] = time.time()
    if usage:
        m["tokens_in"] += usage.get("prompt_tokens", 0) or 0
        m["tokens_out"] += usage.get("completion_tokens", 0) or 0


# paths do vLLM que o agent repassa — mesmo conjunto que o gateway permite
# (docker/gateway/main.py ALLOWED_V1). Sem esta allowlist aqui TAMBÉM, quem
# descobrisse a URL pública do pod (proxy da RunPod) podia chamar endpoints
# administrativos do vLLM (load/unload_lora_adapter etc.) direto, contornando
# qualquer allowlist que existisse só no gateway — o agent é alcançável
# diretamente, sem passar pelo proxy central.
ALLOWED_V1: dict[str, set[str]] = {
    "chat/completions": {"POST"},
    "completions": {"POST"},
    "embeddings": {"POST"},
    "models": {"GET"},
    "responses": {"POST"},  # Codex CLI (0.59+) só fala essa API
}


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy_vllm(path: str, request: Request, authorization: str | None = Header(None)):
    global total_requests, concurrent_now, concurrent_peak

    allowed_methods = ALLOWED_V1.get(path)
    if not allowed_methods or request.method not in allowed_methods:
        raise HTTPException(status_code=404, detail="not found")

    entry = authenticate(authorization)
    api_key_id = entry.get("api_key_id")
    prefix = entry["key_prefix"]
    account = entry["account_name"]

    body = await request.body()
    total_requests += 1
    concurrent_now += 1
    concurrent_peak = max(concurrent_peak, concurrent_now)
    log_line(api_key_id, prefix, account, f"{request.method} /v1/{path}")

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
                    track_usage(api_key_id, usage)
                    log_line(
                        api_key_id, prefix, account,
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

        # resp.json() reparseia o body do zero a cada chamada (httpx não
        # cacheia) — parsear uma vez só e reusar. Antes disso, uma segunda
        # chamada que falhasse (ex.: 200 com corpo vazio) escapava pro
        # except Exception externo, que decrementava concurrent_now DE NOVO
        # (contador ia pro negativo com o tempo) e devolvia 502 "vLLM
        # indisponível" pro cliente mesmo o vLLM tendo respondido normalmente.
        parsed = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                parsed = resp.json()
            except Exception:
                parsed = None

        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        track_usage(api_key_id, usage)
        log_line(
            api_key_id, prefix, account,
            f"{resp.status_code} · {usage.get('total_tokens', '?') if usage else '?'} tokens",
        )
        if path == "models" and isinstance(parsed, dict):
            # espelha o filtro do gateway (main.py, path == "models"): remove os
            # adapters LoRA "acct-<uuid>" da listagem. O pod é alcançável direto
            # pela URL pública do RunPod; sem este filtro TAMBÉM aqui, um tenant
            # com chave válida enumeraria os account_id de todos os co-tenants
            # que dividem o mesmo pod compartilhado, contornando o gateway.
            parsed["data"] = [
                m for m in parsed.get("data", [])
                if not str(m.get("id", "")).startswith("acct-")
            ]
        return JSONResponse(
            content=parsed if parsed is not None else {"raw": resp.text},
            status_code=resp.status_code,
        )
    except HTTPException:
        concurrent_now -= 1
        raise
    except Exception as e:
        concurrent_now -= 1
        log_line(api_key_id, prefix, account, f"erro: {e}")
        raise HTTPException(status_code=502, detail=f"vLLM indisponível: {e}")
