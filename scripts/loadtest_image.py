#!/usr/bin/env python3
"""
Teste de carga do produto Go/image (FLUX.2 Klein 4B): mede tempo, capacidade e VRAM
do pod de difusão através do GATEWAY, cenário a cenário.

Por que não é uma flag no scripts/loadtest.py: aquele script é chat — pool de
tarefas, SSE, `max_tokens`, `finish_reason`, hit rate de prefix cache. Nada
disso existe aqui. O que se repete dele (e é importado, não copiado) é o
percentil, e o que se imita é a disciplina de erro por fase.

O que este script existe para responder, e que nenhuma medida de ponta a ponta
respondia:

  1. Uma geração é lenta por causa do MODELO ou do UPLOAD? O bloco
     `meta.timings` que o pod passou a devolver separa fila, decode, GPU e
     encode; o resto do tempo é `overhead_s` (ver a nota abaixo, que é honesta
     sobre o que há lá dentro).
  2. Quanto o teto de IMAGE_MAX_FILE_SIZE_MB custa. Rodar o MESMO cenário com
     referências de 18 KiB e de 5 MiB isola os segundos que são só rede.
  3. Quantas gerações por minuto o pod entrega de fato — que é o número do qual
     a capacidade física e o teto comercial de submissão devem ser comparados.
  4. Quanta VRAM cada combinação pede (com --admin-url), que é o que decide se
     a placa pode ser menor que uma A40.

SEMPRE pelo gateway, nunca no public_url do pod: bater direto não atualiza
`last_activity_at` e o idle-reaper pode pausar a máquina no meio do teste (ver
armadilha 1 do docs/load-testing-playbook.md). Com os timings vindo dentro da
resposta, não há mais motivo para bypassar o gateway.

Uso:
  python3 scripts/loadtest_image.py \
    --base-url https://api.trystac.com \
    --api-key <chave HEX da stack Image> \
    --model flux2-klein-4b \
    --sizes 1024x1024,1024x1536 \
    --refs 0,1,4 \
    --ref-bytes 18k,5m \
    --levels 1,2,4,8 \
    --admin-url https://<pod>-8000.proxy.runpod.net \
    --admin-secret <AGENT_ADMIN_SECRET> \
    --out image_loadtest.json

Requer: pip install httpx pillow
"""

import argparse
import asyncio
import io
import json
import os
import random
import sys
import time

import httpx

# _pct vem do loadtest de chat em vez de ser copiado: é a mesma interpolação de
# percentil sobre amostras pequenas, e duas cópias divergem em silêncio. O
# insert no sys.path cobre o caso de o script ser chamado de outro diretório —
# o `main` de lá está sob __main__, então importar não executa nada.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loadtest import _pct  # noqa: E402

GENERATIONS_PATH = "/v1/images/generations"
EDITS_PATH = "/v1/images/edits"

# Prompt curto de propósito. O teste do TryStac já mostrou que prompt longo
# destrói a identidade da peça, e IMAGE_MAX_SEQUENCE_LENGTH=512 trunca em
# silêncio — um prompt gigante aqui mediria a truncagem, não a geração.
DEFAULT_PROMPT = "uma camisa social branca dobrada sobre uma mesa de madeira"


# ---------------------------------------------------------------------------
# Referências sintéticas
# ---------------------------------------------------------------------------


def _noise_image(dim: int, seed: int):
    """Imagem de `dim`×`dim` que comprime como FOTO, não como ruído puro.

    Isto não é preciosismo. Ruído branco em PNG ocupa ~3 bytes por pixel; uma
    foto ocupa ~1. Mirar em 14 MiB com ruído puro produziria uma imagem com um
    terço dos pixels de uma foto de 14 MiB — e como o pipeline redimensiona a
    referência de todo jeito, o custo de decode escala com PIXEL, não com byte.
    O teste subestimaria justamente a fase que quer medir.

    Ruído em 1/8 da escala, ampliado por bicúbico, tem estrutura em escala e
    cai na ordem de grandeza certa de bytes por pixel.
    """
    # import aqui dentro, e não no topo: com --ref-file as referências vêm de
    # arquivos reais e esta função nunca roda — o script passa a funcionar numa
    # máquina que só tem httpx.
    from PIL import Image

    small = max(dim // 8, 8)
    rng = random.Random(seed)
    base = Image.frombytes("RGB", (small, small), rng.randbytes(small * small * 3))
    return base.resize((dim, dim), Image.BICUBIC)


def make_reference(target_bytes: int, fmt: str, seed: int = 1) -> bytes:
    """PNG/JPEG sintético com tamanho próximo de `target_bytes`.

    Busca por dimensão: o tamanho do arquivo cresce com a ÁREA, então cada
    iteração corrige por sqrt(alvo/atual). Converge em 2-3 passos; o teto de 5
    existe para o caso patológico (formato que satura, alvo menor que o
    cabeçalho) em que ela nunca chegaria a ±10%.
    """
    dim = max(int((target_bytes / 1.2) ** 0.5), 64)
    data = b""
    for _ in range(5):
        buf = io.BytesIO()
        img = _noise_image(dim, seed)
        if fmt == "jpeg":
            img.save(buf, format="JPEG", quality=92)
        else:
            img.save(buf, format="PNG")
        data = buf.getvalue()
        ratio = target_bytes / max(len(data), 1)
        if 0.9 <= ratio <= 1.1:
            break
        dim = max(int(dim * (ratio ** 0.5)), 64)
    return data


def parse_bytes(raw: str) -> int:
    """'18k' -> 18432, '5m' -> 5242880, '1024' -> 1024."""
    text = raw.strip().lower()
    mult = 1
    if text.endswith("k"):
        mult, text = 1024, text[:-1]
    elif text.endswith("m"):
        mult, text = 1024 * 1024, text[:-1]
    return int(float(text) * mult)


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}GiB"


# ---------------------------------------------------------------------------
# Cenários
# ---------------------------------------------------------------------------


class Scenario:
    """Uma combinação (resolução, nº de referências, tamanho de referência).

    `refs=0` é a rota /v1/images/generations (text-to-image, corpo JSON);
    `refs>0` é /v1/images/edits (multipart). São rotas com custo de rede
    diferente por construção, e é por isso que o cenário — e não a request — é
    a unidade de comparação.

    `ref_datas` já vem com exatamente `refs` itens, montada por
    build_scenarios: quem monta a request não decide quantas referências manda.
    """

    def __init__(self, size: str, ref_datas: list[bytes], rotulo: str = ""):
        self.size = size
        self.ref_datas = ref_datas
        self.refs = len(ref_datas)
        self.rotulo = rotulo

    @property
    def key(self) -> str:
        if self.refs == 0:
            return f"{self.size}/t2i"
        return f"{self.size}/refs={self.refs}/{self.rotulo}"

    @property
    def upload_bytes(self) -> int:
        return sum(len(d) for d in self.ref_datas)


def build_scenarios(args, real_files: list[bytes]) -> list[Scenario]:
    """Produto cartesiano de sizes × refs × ref-bytes, sem a duplicata do t2i.

    `refs=0` não carrega arquivo nenhum, então repeti-lo por tamanho de
    referência rodaria o MESMO teste N vezes — custo de GPU gasto em dado
    redundante.

    Com --ref-file, os arquivos reais substituem a busca sintética e a dimensão
    "tamanho de referência" desaparece: o tamanho passa a ser o dos arquivos.
    Quando há menos arquivos que referências pedidas, eles se repetem em ciclo
    — o pod não deduplica, então N cópias do mesmo arquivo custam N uploads e N
    decodes, que é o que o cenário quer medir.
    """
    scenarios: list[Scenario] = []
    cache: dict[int, bytes] = {}
    for size in args.sizes:
        for refs in args.refs:
            if refs == 0:
                scenarios.append(Scenario(size, []))
                continue
            if real_files:
                datas = [real_files[i % len(real_files)] for i in range(refs)]
                rotulo = f"real:{human(sum(len(d) for d in datas) / refs)}"
                scenarios.append(Scenario(size, datas, rotulo))
                continue
            for target in args.ref_bytes:
                if target not in cache:
                    cache[target] = make_reference(target, args.ref_format)
                data = cache[target]
                scenarios.append(Scenario(size, [data] * refs, human(len(data))))
    return scenarios


# ---------------------------------------------------------------------------
# Requisição
# ---------------------------------------------------------------------------


async def attempt_one(client: httpx.AsyncClient, args, sc: Scenario, seq: int) -> dict:
    """Uma tentativa. Devolve o registro que vai para o JSON.

    `phase` classifica o desfecho, e a classificação importa mais aqui do que
    no teste de chat:

      ok            gerou
      queue_full    429: o pod recusou por lotação. NÃO é falha — é o contrato
                    da fila funcionando, e a contagem dele é o dado de
                    capacidade que o teste veio buscar
      queue_timeout 504: esperou na fila além de IMAGE_QUEUE_WAIT_TIMEOUT_S
      client        4xx nosso (corpo grande demais, formato recusado)
      upstream      5xx do gateway/pod
      connect       nem chegou a mandar — infra do teste, retentável
      read_timeout  mandou e não voltou dentro do teto
    """
    payload_files = None
    body_bytes = 0
    if sc.refs == 0:
        # `model` só no generations: no edits o gateway não aplica pin_model
        # (reescrever o corpo exigiria re-encodar o multipart), e mandar o nome
        # errado ali é 404 do pod.
        body = {"prompt": args.prompt, "size": sc.size, "model": args.model}
        if args.steps:
            body["steps"] = args.steps
        body_bytes = len(json.dumps(body).encode())
        request = {"json": body}
    else:
        ext = "png" if args.ref_format == "png" else "jpg"
        payload_files = [
            ("image[]", (f"ref{i}.{ext}", data, f"image/{args.ref_format}"))
            for i, data in enumerate(sc.ref_datas)
        ]
        data = {"prompt": args.prompt, "size": sc.size}
        if args.steps:
            data["steps"] = str(args.steps)
        body_bytes = sc.upload_bytes
        request = {"data": data, "files": payload_files}

    path = GENERATIONS_PATH if sc.refs == 0 else EDITS_PATH
    t0 = time.monotonic()
    phase = "ok"
    error = None
    timings = None
    image_bytes = None
    status = None
    try:
        resp = await client.post(
            path,
            headers={"Authorization": f"Bearer {args.api_key}"},
            timeout=httpx.Timeout(args.timeout, connect=15.0),
            **request,
        )
        status = resp.status_code
        if status == 200:
            payload = resp.json()
            meta = payload.get("meta") or {}
            timings = meta.get("timings")
            data_items = payload.get("data") or []
            # tamanho da imagem em bytes reais, não do base64: é o número
            # comparável com o que o bucket guarda
            image_bytes = sum(
                (len(item.get("b64_json") or "") * 3) // 4 for item in data_items
            )
            if not data_items:
                phase, error = "upstream", "200 sem `data`"
        else:
            detail = resp.text[:300]
            try:
                code = ((resp.json() or {}).get("error") or {}).get("code")
            except ValueError:
                code = None
            if status == 429:
                phase = "queue_full"
            elif status == 504 or code == "queue_timeout":
                phase = "queue_timeout"
            elif 400 <= status < 500:
                phase = "client"
            else:
                phase = "upstream"
            error = f"HTTP {status} {code or ''}: {detail}"
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
        phase, error = "connect", f"{type(e).__name__}: {e}"
    except Exception as e:
        # ReadTimeout entra aqui e NÃO é retentado (ver run_one): diferente do
        # teste de chat, uma request de imagem que não respondeu pode estar com
        # a GPU ocupada gerando — retentar dobraria a carga real e o relatório
        # mediria um teste diferente do que diz medir.
        phase, error = "read_timeout", f"{type(e).__name__}: {e}"

    total_s = time.monotonic() - t0
    record = {
        "scenario": sc.key,
        "size": sc.size,
        "refs": sc.refs,
        "upload_bytes": body_bytes,
        "seq": seq,
        "status": status,
        "phase": phase,
        "total_s": round(total_s, 3),
        "image_bytes": image_bytes,
        "error": error,
    }
    if isinstance(timings, dict):
        record["timings"] = timings
        pod_s = (timings.get("queue_wait_s") or 0) + (timings.get("worker_s") or 0)
        # `overhead_s`, e não "rede": além de upload e download, aqui dentro
        # está o upload da imagem ao bucket e o insert em image_generations,
        # que o gateway faz ANTES de responder (_persist_images é síncrono no
        # caminho da requisição). Chamar isto de rede seria mentir sobre o que
        # está sendo medido.
        record["overhead_s"] = round(total_s - pod_s, 3)
    return record


async def run_one(client: httpx.AsyncClient, args, sc: Scenario, seq: int) -> dict:
    """Retenta só o que não chegou a sair. Ver attempt_one sobre read_timeout."""
    record = {}
    for attempt in range(1 + args.retries):
        record = await attempt_one(client, args, sc, seq)
        record["retries_used"] = attempt
        if record["phase"] != "connect":
            break
        if attempt < args.retries:
            await asyncio.sleep(2.0 * (attempt + 1))
    return record


async def run_user(client, args, sc: Scenario, user_idx: int, level: int) -> list[dict]:
    out = []
    for seq in range(1, args.requests_per_user + 1):
        r = await run_one(client, args, sc, seq)
        r.update({"level": level, "user": user_idx})
        out.append(r)
        timings = r.get("timings") or {}
        detalhe = ""
        if timings:
            detalhe = (
                f" gpu={timings.get('gpu_s')}s decode={timings.get('decode_s')}s "
                f"fila={timings.get('queue_wait_s')}s over={r.get('overhead_s')}s"
            )
        print(
            f"    [{sc.key} lvl{level} u{user_idx} #{seq}] "
            f"{r['phase']} total={r['total_s']}s{detalhe}",
            flush=True,
        )
    return out


async def run_level(client, args, sc: Scenario, level: int) -> list[dict]:
    t0 = time.monotonic()
    grupos = await asyncio.gather(
        *[run_user(client, args, sc, u, level) for u in range(level)]
    )
    flat = [r for grupo in grupos for r in grupo]
    elapsed = time.monotonic() - t0
    print_level_summary(sc, level, flat, elapsed)
    return flat


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------


def _phase_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["phase"]] = counts.get(r["phase"], 0) + 1
    return counts


def print_level_summary(sc: Scenario, level: int, rows: list[dict], elapsed: float) -> None:
    ok = [r for r in rows if r["phase"] == "ok"]
    counts = _phase_counts(rows)
    totals = [r["total_s"] for r in ok]
    gpus = [r["timings"]["gpu_s"] for r in ok if (r.get("timings") or {}).get("gpu_s")]
    overs = [r["overhead_s"] for r in ok if r.get("overhead_s") is not None]
    # Vazão do CONJUNTO — é o número comparável com RATE_LIMIT_RPM, que o tempo
    # por request sozinho não dá.
    #
    # Só quando NINGUÉM foi recusado: um 429 volta em milissegundos, então uma
    # rajada acima da capacidade encurta o `elapsed` sem ter entregue mais
    # imagem nenhuma. O número sairia inflado justamente no nível em que ele
    # seria lido como "olha quanto o pod aguenta".
    if counts.get("queue_full") or counts.get("queue_timeout"):
        vazao = "img/min n/a (houve recusa)"
    else:
        vazao = f"{60 * len(ok) / elapsed if elapsed > 0 else 0:.1f} img/min"
    linha = (
        f"--- {sc.key} lvl{level}: {len(ok)}/{len(rows)} ok em {elapsed:.1f}s "
        f"({vazao}) · total p50={_pct(totals, 0.5):.1f}s "
        f"p95={_pct(totals, 0.95):.1f}s"
    )
    if gpus:
        linha += f" · gpu p50={_pct(gpus, 0.5):.1f}s"
    if overs:
        linha += f" · overhead p50={_pct(overs, 0.5):.1f}s"
    outros = {k: v for k, v in counts.items() if k != "ok"}
    if outros:
        linha += " · " + " ".join(f"{k}={v}" for k, v in sorted(outros.items()))
    print(linha + " ---", flush=True)


def print_final_report(args, results: list[dict]) -> None:
    print("\n=== resumo por cenário ===", flush=True)
    header = (
        f"{'cenário':<34} {'n':>4} {'ok':>4} {'429':>4} {'total p50':>10} "
        f"{'gpu p50':>8} {'decode p50':>11} {'over p50':>9}"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for key in sorted({r["scenario"] for r in results}):
        rows = [r for r in results if r["scenario"] == key]
        ok = [r for r in rows if r["phase"] == "ok"]
        counts = _phase_counts(rows)
        totals = [r["total_s"] for r in ok]
        gpus = [r["timings"]["gpu_s"] for r in ok if (r.get("timings") or {}).get("gpu_s")]
        decs = [
            r["timings"]["decode_s"] for r in ok
            if (r.get("timings") or {}).get("decode_s") is not None
        ]
        overs = [r["overhead_s"] for r in ok if r.get("overhead_s") is not None]
        print(
            f"{key:<34} {len(rows):>4} {len(ok):>4} {counts.get('queue_full', 0):>4} "
            f"{_pct(totals, 0.5):>9.1f}s {_pct(gpus, 0.5):>7.1f}s "
            f"{_pct(decs, 0.5):>10.2f}s {_pct(overs, 0.5):>8.1f}s",
            flush=True,
        )

    negativos = [r for r in results if (r.get("overhead_s") or 0) < 0]
    if negativos:
        # Impossível no pod real: o relógio de `queue_wait_s` começa quando o
        # job entra na fila e o de `worker_s` termina antes de a resposta ser
        # serializada, então a soma é sempre menor que o tempo do cliente. Um
        # negativo é resposta inconsistente, e some se for mascarado com um
        # max(0, ...) — por isso é avisado, não corrigido.
        pior = min(r["overhead_s"] for r in negativos)
        print(
            f"\n!!! overhead NEGATIVO em {len(negativos)} de {len(results)} "
            f"requests (pior: {pior:.3f}s): o pod reportou mais tempo do que o "
            "cliente mediu. Investigar antes de usar estes números.",
            flush=True,
        )

    ok_total = [r for r in results if r["phase"] == "ok"]
    sem_timings = [r for r in ok_total if not r.get("timings")]
    if sem_timings:
        # O pod anterior à 0.1.3 responde 200 sem o bloco. Sem este aviso, as
        # colunas de fase apareceriam zeradas e seriam lidas como "instantâneo".
        print(
            f"\n!!! meta.timings ausente em {len(sem_timings)} de {len(ok_total)} "
            "respostas OK — a máquina está numa imagem anterior à 0.1.3. As "
            "colunas de fase acima estão vazias por isso, não por serem rápidas.",
            flush=True,
        )


def parse_metrics(text: str) -> dict[str, float]:
    """Texto Prometheus -> {linha da série: valor}. Ignora HELP/TYPE."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


def print_vram_report(before: str | None, after: str | None) -> None:
    if not after:
        return
    metrics = parse_metrics(after)
    print("\n=== VRAM (do /admin/vllm-metrics do pod) ===", flush=True)
    for name in (
        "image_vram_total_bytes",
        "image_vram_device_used_bytes",
        "image_vram_max_reserved_bytes",
    ):
        if name in metrics:
            print(f"  {name:<34} {human(metrics[name])}", flush=True)
    picos = {k: v for k, v in metrics.items() if k.startswith("image_vram_peak_bytes{")}
    if picos:
        print("  pico por cenário:", flush=True)
        for name in sorted(picos):
            rotulo = name[len("image_vram_peak_bytes"):]
            print(f"    {rotulo:<34} {human(picos[name])}", flush=True)
    else:
        print(
            "  (sem série por cenário: ou nenhuma geração passou por este pod, "
            "ou a imagem é anterior à 0.1.3)",
            flush=True,
        )


async def scrape_admin(args) -> str | None:
    """Lê o /metrics do servidor de imagem pelo /admin/vllm-metrics do agent.

    Direto no pod de propósito, e sem risco: a rota é administrativa e não gera
    carga. O TRÁFEGO do teste continua indo pelo gateway, que é o que mantém o
    `last_activity_at` fresco e impede a auto-pausa no meio da medição.
    """
    if not args.admin_url or not args.admin_secret:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(
                args.admin_url.rstrip("/") + "/admin/vllm-metrics",
                headers={"X-Admin-Secret": args.admin_secret},
            )
            if r.status_code != 200:
                print(f"!!! scrape do admin falhou: HTTP {r.status_code}", flush=True)
                return None
            return (r.json() or {}).get("metrics")
    except Exception as e:
        print(f"!!! scrape do admin falhou: {type(e).__name__}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="https://api.trystac.com")
    parser.add_argument("--api-key", help="chave HEX da stack Image")
    parser.add_argument("--model", default="flux2-klein-4b")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--steps", type=int, default=0, help="0 = default do template")
    parser.add_argument(
        "--sizes", default="1024x1024",
        help="resoluções da allowlist do template, separadas por vírgula",
    )
    parser.add_argument(
        "--refs", default="0,1,4",
        help="nº de referências por request. 0 = /v1/images/generations "
        "(text-to-image); >0 = /v1/images/edits (multipart)",
    )
    parser.add_argument(
        "--ref-bytes", default="18k,5m",
        help="tamanhos de arquivo de referência a testar. O par 18k,5m "
        "permite comparar referências leves e próximas do teto comercial.",
    )
    parser.add_argument("--ref-format", default="png", choices=("png", "jpeg"))
    parser.add_argument(
        "--ref-file", action="append",
        help="usa uma imagem REAL como referência em vez da sintética (pode "
        "repetir). Ignora --ref-bytes: o tamanho passa a ser o do arquivo.",
    )
    parser.add_argument(
        "--levels", default="1,4",
        help="níveis de concorrência. IMAGE_QUEUE_CAPACITY é 3 no template "
        "atual, então um nível acima disso é o que mede o 429.",
    )
    parser.add_argument("--requests-per-user", type=int, default=2)
    parser.add_argument(
        "--timeout", type=float, default=180.0,
        help="teto por request. 4 referências grandes já levaram 66s.",
    )
    parser.add_argument(
        "--retries", type=int, default=2,
        help="retentativas para falha de CONEXÃO. Timeout de leitura nunca é "
        "retentado: a GPU pode estar ocupada gerando.",
    )
    parser.add_argument("--admin-url", help="URL pública do agent do pod (porta 8000)")
    parser.add_argument("--admin-secret", help="AGENT_ADMIN_SECRET da máquina")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="monta os cenários, gera as referências e imprime o plano do teste "
        "sem mandar uma request sequer.",
    )
    parser.add_argument("--out", default="image_loadtest.json")
    args = parser.parse_args()

    args.sizes = [s.strip() for s in args.sizes.split(",") if s.strip()]
    args.refs = sorted({int(x) for x in args.refs.split(",") if x.strip()})
    args.ref_bytes = [parse_bytes(x) for x in args.ref_bytes.split(",") if x.strip()]
    args.levels = [int(x) for x in args.levels.split(",") if x.strip()]
    if not args.dry_run and not args.api_key:
        parser.error("--api-key é obrigatório fora do --dry-run")

    real_files: list[bytes] = []
    for path in args.ref_file or []:
        with open(path, "rb") as f:
            real_files.append(f.read())

    scenarios = build_scenarios(args, real_files)
    total_requests = sum(
        level * args.requests_per_user for level in args.levels
    ) * len(scenarios)

    print("=== plano do teste ===", flush=True)
    for sc in scenarios:
        upload = human(sc.upload_bytes) if sc.refs else "—"
        print(f"  {sc.key:<34} upload/request={upload}", flush=True)
    print(
        f"  {len(scenarios)} cenários × níveis {args.levels} × "
        f"{args.requests_per_user} req/usuário = {total_requests} requests",
        flush=True,
    )
    print(
        "  nota: cada geração é persistida no bucket pelo gateway, com "
        "expires_at de 30 dias.",
        flush=True,
    )
    if args.dry_run:
        print("\n(--dry-run: nada foi enviado)", flush=True)
        return

    before = await scrape_admin(args)

    peak = max(args.levels)
    # keepalive desligado pelo mesmo motivo do loadtest de chat: conexão morta
    # no pool vira request que pendura até o timeout sem chegar ao servidor
    # (armadilha 5 do playbook).
    limits = httpx.Limits(max_connections=peak * 2, max_keepalive_connections=0)
    transport = httpx.AsyncHTTPTransport(retries=2)
    results: list[dict] = []
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), limits=limits, transport=transport
    ) as client:
        for sc in scenarios:
            print(f"\n=== cenário {sc.key} ===", flush=True)
            for level in args.levels:
                results.extend(await run_level(client, args, sc, level))
                with open(args.out, "w") as f:
                    json.dump({"results": results}, f, indent=2, ensure_ascii=False)

    after = await scrape_admin(args)
    print_final_report(args, results)
    print_vram_report(before, after)

    with open(args.out, "w") as f:
        json.dump(
            {
                "config": {
                    "base_url": args.base_url,
                    "model": args.model,
                    "sizes": args.sizes,
                    "refs": args.refs,
                    "ref_bytes": args.ref_bytes,
                    "ref_format": args.ref_format,
                    "levels": args.levels,
                    "requests_per_user": args.requests_per_user,
                    "prompt": args.prompt,
                },
                "admin_metrics_before": before,
                "admin_metrics_after": after,
                "results": results,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\n=== CONCLUIDO — resultados em {args.out} ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
