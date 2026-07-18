#!/usr/bin/env python3
"""
Teste de carga padrão pra qualquer plano: N "usuários" concorrentes, cada um
mandando um número fixo de requisições sequenciais de chat completion (não em
paralelo consigo mesmo), amostradas de um pool de tarefas com categoria e
dificuldade variadas (coding, debugging, matemática, raciocínio lógico).
Mede tempo total, tokens e erros por requisição via streaming SSE contra o
GATEWAY de produção — nunca direto no pod (ver docs/load-testing-playbook.md
pro porquê).

Uso:
  python3 scripts/loadtest.py \
    --base-url https://llmserve-docker.up.railway.app \
    --api-key <chave HEX da conta> \
    --model pro-base \
    --levels 5,10,15 \
    --requests-per-user 5 \
    --max-tokens 8000 \
    --out resultados.json

Requer: pip install httpx
"""

import argparse
import asyncio
import json
import random
import time

import httpx

TASKS = [
    # ---- coding ----
    {"id": "code_f1", "cat": "coding", "dif": "facil", "prompt": "Escreva uma função em Python que verifica se uma string é um palíndromo (ignorando espaços, pontuação e maiúsculas/minúsculas)."},
    {"id": "code_f2", "cat": "coding", "dif": "facil", "prompt": "Escreva uma função em JavaScript que retorna o maior número de uma lista, sem usar Math.max."},
    {"id": "code_m1", "cat": "coding", "dif": "medio", "prompt": "Implemente uma fila (queue) usando duas pilhas (stacks) em Python, com enqueue e dequeue em complexidade amortizada O(1)."},
    {"id": "code_m2", "cat": "coding", "dif": "medio", "prompt": "Implemente busca binária iterativa em um array ordenado, tratando corretamente o caso de elemento não encontrado, com testes."},
    {"id": "code_d1", "cat": "coding", "dif": "dificil", "prompt": "Implemente um parser de expressões aritméticas com parênteses e precedência de operadores (+, -, *, /), retornando o resultado numérico."},
    {"id": "code_d2", "cat": "coding", "dif": "dificil", "prompt": "Implemente compressão Run-Length Encoding (RLE) e a descompressão correspondente, cobrindo casos extremos (string vazia, sem repetição, tudo repetido)."},
    # ---- debugging ----
    {"id": "bug_f1", "cat": "debugging", "dif": "facil", "prompt": "Esse código Python tem um bug de off-by-one, encontre e corrija:\n\n```python\ndef primeiros_n(lista, n):\n    resultado = []\n    for i in range(n + 1):\n        resultado.append(lista[i])\n    return resultado\n```"},
    {"id": "bug_m1", "cat": "debugging", "dif": "medio", "prompt": "Esse código Python tem um bug clássico de argumento mutável default, encontre e corrija, explicando por que acontece:\n\n```python\ndef adiciona_item(item, lista=[]):\n    lista.append(item)\n    return lista\n```"},
    {"id": "bug_d1", "cat": "debugging", "dif": "dificil", "prompt": "Esse código Python trava em deadlock às vezes. Encontre a causa e corrija:\n\n```python\nimport asyncio\n\nlock_a = asyncio.Lock()\nlock_b = asyncio.Lock()\n\nasync def task1():\n    async with lock_a:\n        await asyncio.sleep(0.1)\n        async with lock_b:\n            print('task1 done')\n\nasync def task2():\n    async with lock_b:\n        await asyncio.sleep(0.1)\n        async with lock_a:\n            print('task2 done')\n```"},
    {"id": "bug_d2", "cat": "debugging", "dif": "dificil", "prompt": "Esse código JavaScript vaza memória num app de longa duração (SPA). Encontre a causa e corrija:\n\n```javascript\nfunction setupWidget(el) {\n  const cache = [];\n  function onClick() {\n    cache.push(el.innerText);\n    console.log(cache.length);\n  }\n  el.addEventListener('click', onClick);\n}\n// setupWidget é chamado toda vez que o widget é recriado, sem nunca remover o listener antigo\n```"},
    # ---- matemática ----
    {"id": "math_f1", "cat": "matematica", "dif": "facil", "prompt": "Quanto é a soma de todos os números inteiros de 1 a 100? Mostre o raciocínio."},
    {"id": "math_m1", "cat": "matematica", "dif": "medio", "prompt": "Resolva a equação quadrática 2x² - 5x + 3 = 0, mostrando os passos pela fórmula de Bhaskara."},
    {"id": "math_d1", "cat": "matematica", "dif": "dificil", "prompt": "Calcule a probabilidade de tirar exatamente 3 caras em 5 lançamentos de uma moeda justa, mostrando a fórmula binomial usada e o cálculo completo."},
    {"id": "math_d2", "cat": "matematica", "dif": "dificil", "prompt": "Prove que a soma dos ângulos internos de qualquer triângulo é 180°, usando geometria euclidiana (retas paralelas e ângulos alternos internos)."},
    # ---- think / raciocínio lógico ----
    {"id": "think_f1", "cat": "think", "dif": "facil", "prompt": "Se todos os Bloops são Razzies e todos os Razzies são Lazzies, todos os Bloops são Lazzies? Explique o raciocínio passo a passo."},
    {"id": "think_m1", "cat": "think", "dif": "medio", "prompt": "Você tem 3 caixas rotuladas 'Maçãs', 'Laranjas' e 'Maçãs e Laranjas'. Todas as etiquetas estão erradas. Você pode tirar UMA fruta de UMA caixa sem olhar dentro. Como descobrir o conteúdo correto de todas as caixas? Explique o raciocínio."},
    {"id": "think_d1", "cat": "think", "dif": "dificil", "prompt": "100 prisioneiros numerados vão receber um chapéu preto ou branco cada, aleatoriamente. Cada um vê os chapéus dos outros mas não o próprio, e precisa adivinhar a cor do seu em ordem, ouvindo os palpites anteriores. Qual estratégia garante que pelo menos 99 acertem? Explique."},
    {"id": "think_d2", "cat": "think", "dif": "dificil", "prompt": "É possível atravessar as 7 pontes de Königsberg exatamente uma vez cada, retornando ao ponto de partida? Explique por que sim ou não, usando o raciocínio de Euler sobre grafos."},
]


def build_big_context(seed_idx: int, approx_tokens: int) -> str:
    """Gera um bloco de "contexto de projeto" sintético de ~approx_tokens
    tokens, ÚNICO por usuário/conta (seed_idx entra no conteúdo) mas ESTÁVEL
    entre as requisições sequenciais do mesmo usuário — simula uma sessão de
    coding real (mesmos arquivos ao longo da sessão): exercita o
    prefix-caching intra-sessão, mas sem cache cross-user, então cada usuário
    pressiona o KV com seu próprio prefixo (pior caso de ocupação). A
    contagem por token é aproximada (~4 chars/token de código); o
    prompt_tokens REAL de cada request vem no usage e é o que o relatório
    reporta."""
    if approx_tokens <= 0:
        return ""
    target_chars = approx_tokens * 4
    parts = [f"# Projeto interno do usuário {seed_idx} — código-fonte para contexto\n\n"]
    size = len(parts[0])
    i = 0
    while size < target_chars:
        block = (
            f"def modulo_{seed_idx}_func_{i}(entradas, pesos):\n"
            f"    # rotina {seed_idx}.{i} do serviço de faturamento interno\n"
            f"    acumulado = 0\n"
            f"    for k in range(len(entradas)):\n"
            f"        acumulado += entradas[k] * pesos[k % len(pesos)] + {i}\n"
            f"    return acumulado  # marcador único {seed_idx}-{i}\n\n"
        )
        parts.append(block)
        size += len(block)
        i += 1
    return "".join(parts)


def build_user_queue(user_idx: int, level: int, requests_per_user: int) -> list[dict]:
    pool = TASKS.copy()
    rng = random.Random(1000 * level + user_idx)
    rng.shuffle(pool)
    queue = []
    i = 0
    while len(queue) < requests_per_user:
        queue.append(pool[i % len(pool)])
        i += 1
    return queue


async def attempt_one(client: httpx.AsyncClient, args, task: dict, api_key: str, context_prefix: str = "") -> dict:
    """Uma tentativa de request. `error_phase` distingue falha ANTES do
    primeiro byte de resposta (pre_byte: conexão zumbi/reset — infra de
    teste, seguro retentar) de stream cortado no meio (mid_stream: perda
    real, um cliente de verdade veria a resposta truncada). `context_prefix`
    (opcional) enche o prompt com ~30K de contexto pra medir o teto novo."""
    content = task["prompt"]
    if context_prefix:
        content = (
            f"{context_prefix}\n\n---\n\n"
            f"Com base no código acima, responda: {task['prompt']}"
        )
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": args.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.monotonic()
    full_text = ""
    finish_reason = None
    usage = None
    error = None
    first_byte = False
    try:
        async with client.stream(
            "POST", "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            # read = teto de silêncio ENTRE chunks (não duração total): pega
            # tanto o primeiro byte que nunca chega (conexão zumbi) quanto um
            # stream que congelou. Com o filtro de reasoning do gateway, o 1º
            # byte visível só sai quando o raciocínio fecha — dimensionar o
            # default pra isso (120s cobre os ~85s observados no VibeCoder).
            timeout=httpx.Timeout(args.first_byte_timeout, connect=15.0),
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                error = f"HTTP {resp.status_code}: {body[:300]!r}"
            else:
                async for line in resp.aiter_lines():
                    first_byte = True
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        full_text += delta.get("content") or ""
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    total_time = time.monotonic() - t0
    return {
        "task_id": task["id"],
        "cat": task["cat"],
        "dif": task["dif"],
        "total_s": round(total_time, 2),
        "chars": len(full_text),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "finish_reason": finish_reason,
        "error": error,
        "error_phase": None if not error else ("mid_stream" if first_byte else "pre_byte"),
    }


async def run_one(client: httpx.AsyncClient, args, task: dict, api_key: str, tags: dict, context_prefix: str = "") -> dict:
    """Roda a task com retry pra falha pre_byte (nenhum byte recebido —
    nada foi consumido, retentar é seguro). mid_stream NUNCA retenta: um
    stream cortado é perda real que o relatório precisa contar. `tags`
    identifica o registro no resultado (nível/usuário ou modo/conta)."""
    result = None
    for attempt in range(1 + args.retries):
        result = await attempt_one(client, args, task, api_key, context_prefix)
        result.update({**tags, "retries_used": attempt})
        if not result["error"] or result["error_phase"] == "mid_stream":
            break
        if attempt < args.retries:
            await asyncio.sleep(2.0 * (attempt + 1))
    return result


async def run_user(client: httpx.AsyncClient, args, user_idx: int, level: int) -> list[dict]:
    queue = build_user_queue(user_idx, level, args.requests_per_user)
    # contexto grande estável por usuário (único entre usuários) — ver build_big_context
    ctx = build_big_context(1000 * level + user_idx, args.context_tokens)
    out = []
    for seq, task in enumerate(queue, start=1):
        r = await run_one(client, args, task, args.api_key, {"level": level, "user": user_idx, "seq": seq}, ctx)
        out.append(r)
        err_info = f"err={r['error']} phase={r['error_phase']}" if r["error"] else "err=None"
        retry_info = f" retries={r['retries_used']}" if r.get("retries_used") else ""
        print(
            f"    [lvl{level} u{user_idx} #{seq}] {task['id']:<10} "
            f"total={r['total_s']}s tok={r['completion_tokens']} finish={r['finish_reason']} {err_info}{retry_info}",
            flush=True,
        )
    return out


async def run_account(
    client: httpx.AsyncClient, args, api_key: str, account_idx: int, mode: str, queue: list[dict]
) -> list[dict]:
    """Sessão de uma conta no cenário de isolamento: requests sequenciais,
    mesma fila de tasks nos dois modos (comparação justa baseline vs full)."""
    # contexto grande estável por conta (único entre contas) — ver build_big_context
    ctx = build_big_context(5000 + account_idx, args.context_tokens)
    out = []
    for seq, task in enumerate(queue, start=1):
        r = await run_one(
            client, args, task, api_key,
            {"mode": mode, "account": account_idx, "seq": seq},
            ctx,
        )
        out.append(r)
        err_info = f"err={r['error']} phase={r['error_phase']}" if r["error"] else "err=None"
        print(
            f"    [{mode} acct{account_idx} #{seq}] {task['id']:<10} "
            f"total={r['total_s']}s tok={r['completion_tokens']} finish={r['finish_reason']} {err_info}",
            flush=True,
        )
    return out


async def run_isolation(client: httpx.AsyncClient, args) -> list[dict]:
    """Cenário de isolamento de slot (playbook §Metodologia): N contas na
    MESMA máquina, cada uma com sua chave/stack. Baseline = cada conta
    sozinha, em série; full = todas simultâneas. O delta de tempo médio
    por conta entre os modos é o "custo do vizinho"."""
    keys = args.isolation_keys
    # fila fixa por conta (seed própria): as MESMAS tasks nos dois modos
    queues = [build_user_queue(5000 + i, 0, args.requests_per_user) for i in range(len(keys))]
    all_results = []

    print(f"=== isolamento: modo BASELINE ({len(keys)} contas, uma por vez) ===", flush=True)
    for i, key in enumerate(keys):
        all_results.extend(await run_account(client, args, key, i, "baseline", queues[i]))

    print(f"=== isolamento: modo FULL ({len(keys)} contas simultâneas) ===", flush=True)
    full = await asyncio.gather(
        *[run_account(client, args, key, i, "full", queues[i]) for i, key in enumerate(keys)]
    )
    all_results.extend(r for sub in full for r in sub)

    print("\n--- custo do vizinho por conta (tempo médio, só requests OK) ---", flush=True)
    for i in range(len(keys)):
        base = [r["total_s"] for r in all_results if r.get("account") == i and r.get("mode") == "baseline" and not r["error"]]
        cheio = [r["total_s"] for r in all_results if r.get("account") == i and r.get("mode") == "full" and not r["error"]]
        if base and cheio:
            mb, mf = sum(base) / len(base), sum(cheio) / len(cheio)
            print(f"  conta {i}: baseline={mb:.1f}s full={mf:.1f}s delta={100 * (mf - mb) / mb:+.0f}%", flush=True)
        else:
            print(f"  conta {i}: dados insuficientes (erros demais em um dos modos)", flush=True)
    return all_results


async def run_level(client: httpx.AsyncClient, args, level: int) -> list[dict]:
    print(f"=== nivel {level} concorrentes ({level * args.requests_per_user} requests) ===", flush=True)
    t0 = time.monotonic()
    results = await asyncio.gather(*[run_user(client, args, u, level) for u in range(level)])
    elapsed = time.monotonic() - t0
    flat = [r for sub in results for r in sub]
    errors = [r for r in flat if r["error"]]
    pre = sum(1 for r in errors if r["error_phase"] == "pre_byte")
    mid = len(errors) - pre
    retried_ok = sum(1 for r in flat if not r["error"] and r.get("retries_used"))
    print(
        f"--- nivel {level} concluido em {elapsed:.1f}s, {len(flat)} reqs, "
        f"{len(errors)} erros ({pre} pre_byte, {mid} mid_stream), "
        f"{retried_ok} recuperadas por retry ---",
        flush=True,
    )
    return flat


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="URL do gateway (ex: https://llmserve-docker.up.railway.app)")
    parser.add_argument("--api-key", help="Chave HEX da conta/stack sendo testada (modo níveis de concorrência)")
    parser.add_argument(
        "--isolation-keys",
        help="Cenário de isolamento de slot: chaves HEX de N contas DISTINTAS "
        "na mesma máquina, separadas por vírgula. Roda baseline (cada conta "
        "sozinha) e depois full (todas simultâneas) e reporta o delta por "
        "conta. Ignora --levels.",
    )
    parser.add_argument("--model", required=True, help="served-model-name do template (ex: pro-base, vibecoder-base)")
    parser.add_argument("--levels", default="5,10,15", help="Níveis de concorrência, separados por vírgula")
    parser.add_argument("--requests-per-user", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument(
        "--context-tokens", type=int, default=0,
        help="Se >0, prefixa cada prompt com ~N tokens de contexto sintético "
        "(código de projeto) pra medir o teto de contexto do plano no PIOR "
        "caso. Único por usuário (pressiona o KV) mas estável na sessão "
        "(exercita prefix-caching). Ex: 30000 pra validar max-model-len 32768. "
        "A contagem é aproximada; o prompt_tokens real vem no usage.",
    )
    parser.add_argument(
        "--first-byte-timeout", type=float, default=120.0,
        help="Teto de silêncio entre chunks (s). Com o filtro de reasoning do "
        "gateway, o 1º byte visível só sai quando o raciocínio fecha — não "
        "apertar demais (85s+ observados em tarefas difíceis).",
    )
    parser.add_argument(
        "--retries", type=int, default=2,
        help="Retentativas quando NENHUM byte de resposta chegou (conexão "
        "zumbi/reset). Stream cortado no meio nunca é retentado.",
    )
    parser.add_argument("--out", default="loadtest_results.json")
    args = parser.parse_args()
    args.levels = [int(x) for x in args.levels.split(",")]
    args.isolation_keys = args.isolation_keys.split(",") if args.isolation_keys else None
    if not args.api_key and not args.isolation_keys:
        parser.error("informe --api-key (modo níveis) ou --isolation-keys (modo isolamento)")

    # keepalive DESLIGADO de propósito: após um blip de rede, conexões
    # keepalive mortas no pool são reutilizadas como "zumbis" — o request é
    # escrito numa conexão morta e pendura até o timeout sem nunca chegar ao
    # servidor (visto no teste do Pro em 17/07/2026: 7 requests de 600s).
    # Conexão nova por request custa ~1 handshake TLS (<300ms), irrelevante
    # em requests de 60-600s.
    peak = len(args.isolation_keys) if args.isolation_keys else max(args.levels)
    limits = httpx.Limits(max_connections=peak * 2, max_keepalive_connections=0)
    transport = httpx.AsyncHTTPTransport(retries=2)
    all_results = []
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), limits=limits, transport=transport) as client:
        if args.isolation_keys:
            all_results = await run_isolation(client, args)
            with open(args.out, "w") as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)
        else:
            for level in args.levels:
                level_results = await run_level(client, args, level)
                all_results.extend(level_results)
                with open(args.out, "w") as f:
                    json.dump(all_results, f, indent=2, ensure_ascii=False)
                print(f"(salvo parcial: {len(all_results)} resultados até agora)\n", flush=True)

    print(f"\n=== TUDO CONCLUIDO — resultados em {args.out} ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
