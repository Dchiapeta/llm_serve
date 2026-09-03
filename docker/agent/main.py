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

from proxy_policy import (
    ALLOWED_V1,
    UnparseableBody,
    merge_key_entry,
    prepare_proxy_body,
    upstream_content_type_for,
)
from usage_norm import SseUsageScanner, usage_from_event

VLLM_URL = os.environ.get("VLLM_URL", "http://127.0.0.1:8001")
ADMIN_SECRET = os.environ.get("AGENT_ADMIN_SECRET", "")
MODEL_NAME = os.environ.get("MODEL_NAME", "")
# teto de usuários definido pelo painel na criação do pod; 0 = sem teto.
# O enforcement real é do painel (emissão de chaves) — aqui é só informativo.
MAX_USERS = int(os.environ.get("MAX_USERS", "0") or 0)
VLLM_LOG_FILE = os.environ.get("VLLM_LOG_FILE", "/var/log/vllm.log")
# Substring procurada em /proc/*/cmdline para saber se o servidor de inferência
# irmão está vivo (ver _vllm_process_alive). O default é a linha de comando do
# vLLM; o pod de imagem sobrescreve com o caminho do server.py dele
# (docker/image/entrypoint.sh). Como é comparada contra bytes, encoda aqui uma
# vez em vez de a cada processo varrido.
SERVER_PROCESS_MATCH = os.environ.get(
    "SERVER_PROCESS_MATCH", "vllm.entrypoints.openai.api_server"
).encode()
# diretório local onde adapters LoRA baixados do storage ficam antes do load
LORA_DIR = os.environ.get("LORA_DIR", "/workspace/loras")

# Isolamento de prefix cache entre tenants do mesmo pod (ver proxy_policy.py).
# Contrato de DUAS variáveis com o entrypoint: esta MESMA env é o que liga o
# --enable-prefix-caching no vLLM (docker/entrypoint.sh) e o que prova que o
# agent desta imagem sabe isolar. As duas viajam no mesmo container, então
# nenhuma ordem de deploy consegue ligar o caching sem o salt:
#   imagem velha + var no template -> vLLM antigo ignora a var, caching off
#   imagem nova  + var ausente     -> caching off, salt off
# Qualquer valor diferente de "cache_salt" deixa tudo desligado (fail-closed).
SALT_ENABLED = (
    os.environ.get("PREFIX_CACHE_ISOLATION", "").strip().lower() == "cache_salt"
)

STARTED_AT = time.time()

# chaves sincronizadas: hash -> entrada montada por proxy_policy.merge_key_entry
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
    if SALT_ENABLED:
        if ADMIN_SECRET:
            print("[agent] prefix cache isolado por cache_salt (salt por stack)")
        else:
            # sem segredo não há salt derivável; o entrypoint já terá ligado o
            # caching, então o pod fica COM caching e SEM isolamento — grita.
            print(
                "[agent] ATENÇÃO: PREFIX_CACHE_ISOLATION=cache_salt sem "
                "AGENT_ADMIN_SECRET — nenhum salt será injetado"
            )
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
    # snapshot ANTES do clear(): merge_key_entry preserva o stack_id conhecido
    # quando o payload não traz (produtor antigo), e sem isso o salt do tenant
    # trocaria de ident a cada sync, zerando o cache dele. O clear() continua
    # necessário — é como uma chave revogada sai da memória do agent.
    previous = dict(keys_by_hash)
    keys_by_hash.clear()
    for k in body.keys:
        # .get() em vez de indexação direta: um registro sem key_hash não
        # deve estourar KeyError no meio do loop (já rodou o clear() acima —
        # um erro aqui deixaria keys_by_hash só com os registros anteriores
        # a esse, derrubando 401 pra todo mundo que vinha depois na lista)
        key_hash = k.get("key_hash")
        if not key_hash:
            continue
        keys_by_hash[key_hash] = merge_key_entry(previous.get(key_hash), k, key_hash)
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
    return {
        "ok": True,
        "model": MODEL_NAME,
        "max_users": MAX_USERS,
        # o painel/gateway conseguem confirmar que ESTE pod está isolando sem
        # ter que ler o log de boot
        "prefix_cache_isolation": "cache_salt" if SALT_ENABLED else None,
    }


# Métricas cruas do vLLM (formato Prometheus), incluindo
# vllm:prefix_cache_hits / vllm:prefix_cache_queries — é como se mede o hit
# rate real do pod. Fica SOB require_admin e NUNCA em /v1/*: os contadores são
# do processo inteiro e agregam todos os co-tenants; expor a um cliente
# devolveria justamente a informação de vizinho que o cache_salt existe pra
# negar.
@app.get("/admin/vllm-metrics")
async def vllm_metrics(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    try:
        resp = await client.get("/metrics", timeout=10.0)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"vLLM indisponível: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"vLLM /metrics falhou: {resp.status_code}")
    return {"metrics": resp.text}


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
        keys_by_hash[key_hash] = merge_key_entry(keys_by_hash.get(key_hash), k, key_hash)
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


class TokenizeBody(BaseModel):
    text: str
    model: str


# Contagem real de tokens via /tokenize do vLLM (rota na raiz, sem prefixo
# /v1 — confirmado no código-fonte da v0.24.0, vllm/entrypoints/serve/tokenize).
# Usado pelo gateway (context_budget) só perto do limite de contexto, pra
# decidir aceitar/rejeitar com a mesma contagem que o vLLM realmente usa —
# não pela allowlist /v1/* client-facing, já que é uma chamada interna do
# gateway sem uma Bearer key de conta associada.
@app.post("/admin/tokenize")
async def admin_tokenize(body: TokenizeBody, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    try:
        resp = await client.post(
            "/tokenize", json={"model": body.model, "prompt": body.text}, timeout=10.0
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"vLLM tokenize indisponível: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"vLLM tokenize falhou: {resp.text}")
    count = resp.json().get("count")
    if not isinstance(count, int):
        raise HTTPException(status_code=502, detail="vLLM tokenize: resposta sem contagem")
    return {"count": count}


# ---------- Health público ----------


@app.get("/")
async def root():
    # o proxy/health-check do RunPod bate na raiz; sem esta rota ele recebia 404
    # e reiniciava o pod em loop, matando o vLLM antes de carregar o modelo.
    return {"ok": True, "service": "agent"}


def _vllm_process_alive() -> bool:
    # O entrypoint sobe o servidor de inferência como processo irmão; se ele
    # morrer (ex.: OOM na inicialização), o pod continua RUNNING mas nunca
    # ficará pronto.
    #
    # A agulha é configurável porque esta MESMA imagem de agent roda na frente
    # de dois servidores diferentes: o vLLM (default) e o servidor de difusão
    # (docker/image/server.py, que exporta SERVER_PROCESS_MATCH no entrypoint).
    # Com a string do vLLM fixa aqui, um pod de imagem saudável reportaria
    # vllm_alive=false durante todo o boot e o painel mostraria "Falha" enquanto
    # ele apenas baixava os pesos.
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if SERVER_PROCESS_MATCH in cmdline:
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


def cached_tokens_of(usage: dict | None) -> int:
    """Tokens de prompt servidos pelo prefix cache nesta request.

    Só existe com --enable-prompt-tokens-details no vLLM (default off); sem a
    flag o campo nunca vem e isto devolve 0 — o que é indistinguível de
    "caching ligado e sem hit nenhum". É por isso que a flag entra junto com
    PREFIX_CACHE_ISOLATION no template: sem ela a verificação do rollout fica
    sem ground truth."""
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return 0
    value = details.get("cached_tokens")
    return value if isinstance(value, int) else 0


def track_usage(api_key_id: str | None, usage: dict | None):
    """`usage` já normalizado pra forma chat pelos chamadores (usage_norm.py) —
    o protocolo Responses nomeia as contagens de outro jeito e chegar aqui
    cru significa somar zero em silêncio."""
    if not api_key_id:
        return  # chave sem id sincronizado (sync antigo) — nada seguro pra agregar
    m = metrics_per_key.setdefault(
        api_key_id,
        {"requests": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
         "last_used": None},
    )
    m["requests"] += 1
    m["last_used"] = time.time()
    if usage:
        m["tokens_in"] += usage.get("prompt_tokens", 0) or 0
        m["tokens_out"] += usage.get("completion_tokens", 0) or 0
        # setdefault: métricas criadas por uma versão anterior do agent (sem
        # esta chave) não podem estourar KeyError no primeiro incremento
        m["tokens_cached"] = m.get("tokens_cached", 0) + cached_tokens_of(usage)


def usage_summary(usage: dict | None) -> str:
    """Sufixo de log com tokens e hit rate de cache, pra /admin/logs."""
    if not usage:
        return "? tokens"
    total = usage.get("total_tokens", "?")
    prompt = usage.get("prompt_tokens") or 0
    cached = cached_tokens_of(usage)
    if prompt and cached:
        return f"{total} tokens · cache {cached}/{prompt} ({cached * 100 // prompt}%)"
    return f"{total} tokens"


# ALLOWED_V1 e a política de cache_salt vivem em proxy_policy.py (importados no
# topo) — funções puras, testáveis sem subir o agent. O teste-guarda de
# test_proxy_policy.py exige que todo POST da allowlist esteja classificado
# como salgado ou isento, pra que um endpoint novo não passe sem isolamento.


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
        # Reescrita do corpo (detecção de streaming + include_usage + salt) em
        # proxy_policy.prepare_proxy_body — módulo puro, testável sem subir o
        # agent. Cuidado histórico: o json.dumps ficava DENTRO do `if
        # is_stream`, então requisição não-streaming reenviava o corpo original
        # e qualquer reescrita era perdida em silêncio.
        try:
            body, is_stream = prepare_proxy_body(
                body, path, entry, salt_enabled=SALT_ENABLED, secret=ADMIN_SECRET
            )
        except UnparseableBody:
            raise HTTPException(status_code=400, detail="corpo inválido (JSON esperado)")

        # Content-Type: repassa o do cliente APENAS quando é multipart.
        #
        # O /v1/images/edits do pod de imagem é multipart/form-data, e é nesse
        # header que viaja o `boundary=...` que delimita as partes —
        # sobrescrevê-lo por "application/json" torna o corpo impossível de
        # parsear do outro lado.
        #
        # Fora do multipart o header continua FORÇADO em application/json, e não
        # repassado. Repassar sempre seria uma regressão silenciosa: cliente que
        # manda corpo JSON declarando "text/plain" (acontece em HTTP client
        # simples) funciona hoje justamente porque o agent corrige o header
        # aqui; com o repasse cru ele passaria a tomar 422 do vLLM.
        client_content_type = request.headers.get("content-type", "")
        upstream_content_type = upstream_content_type_for(client_content_type)

        if is_stream:
            upstream_req = client.build_request(
                request.method, f"/v1/{path}", content=body,
                headers={"Content-Type": upstream_content_type},
            )
            upstream = await client.send(upstream_req, stream=True)

            async def stream_and_close():
                # Scanner linha a linha, e não a janela dos últimos 16 KiB do
                # stream que existia aqui: no protocolo Responses o usage viaja
                # no evento final "response.completed", que carrega o snapshot
                # INTEIRO da resposta (todo o texto gerado). Numa resposta longa
                # esse evento passa de 16 KiB sozinho, a janela cortava o JSON
                # no meio e o parse falhava — usage_metrics ficava zerado mesmo
                # depois de acertar o caminho do campo.
                scanner = SseUsageScanner()
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                        scanner.feed(chunk)
                finally:
                    await upstream.aclose()
                    usage = scanner.finish()
                    global concurrent_now
                    concurrent_now -= 1
                    track_usage(api_key_id, usage)
                    log_line(
                        api_key_id, prefix, account,
                        f"stream concluído ({upstream.status_code}) · "
                        f"{usage_summary(usage)}",
                    )

            return StreamingResponse(
                stream_and_close(),
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "text/event-stream"),
            )

        resp = await client.request(
            request.method, f"/v1/{path}", content=body,
            headers={"Content-Type": upstream_content_type},
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

        # usage_from_event e não parsed.get("usage"): em /v1/responses o dict
        # chega com input_tokens/output_tokens, que o track_usage abaixo não
        # sabe ler (contava request e zero token)
        usage = usage_from_event(parsed)
        track_usage(api_key_id, usage)
        log_line(
            api_key_id, prefix, account,
            f"{resp.status_code} · {usage_summary(usage)}",
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
