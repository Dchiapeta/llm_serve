#!/usr/bin/env python3
"""
Bateria de checks de hardening do gateway: confirma que cada defesa
(allowlist, pinning de modelo, limites, rate limit/concorrência, headers,
admin, tradução Anthropic) efetivamente BARRA o que deveria barrar — e que
tráfego legítimo continua passando normalmente. Roda contra o GATEWAY de
produção, nunca direto no pod (mesma regra do loadtest.py).

Uso (checks rápidos, sem custo de rajada):
  python3 scripts/security_check.py \
    --base-url https://llmserve-docker.up.railway.app \
    --api-key <chave HEX de uma conta de TESTE> \
    --model <served-model-name do template, ex: pro-base>

Uso (inclui rate limit/concorrência — CUSTO REAL DE GPU, ver aviso abaixo):
  python3 scripts/security_check.py --base-url ... --api-key ... --model ... \
    --include-burst

Aviso sobre --include-burst: o piso de max_tokens do gateway (MIN_MAX_TOKENS,
8000) se aplica a TODA requisição que passa da validação — os checks de
rate-limit/concorrência disparam dezenas de requisições de propósito, e as
que NÃO forem barradas seguem pra inferência real (o modelo ainda para
sozinho num prompt curto, mas é geração de verdade, não simulada). Use uma
conta/stack de TESTE, nunca uma de cliente real, e prefira rodar numa
máquina já acordada (senão o primeiro burst ainda paga o custo de auto-wake).

Requer: pip install httpx
"""

import argparse
import asyncio

import httpx

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)


# ---------- checks sempre rodados (baratos/rápidos) ----------


async def check_health_and_headers(client: httpx.AsyncClient) -> None:
    """Regressão do bug crítico do middleware de headers: response.headers
    .pop() não existe no MutableHeaders do Starlette e derrubava TODA
    requisição com 500. Se isso falhar, o gateway inteiro está fora do ar."""
    r = await client.get("/health")
    record("/health responde 200 (regressão do bug crítico do middleware)",
           r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        record("header X-Content-Type-Options presente",
               r.headers.get("x-content-type-options") == "nosniff",
               f"valor={r.headers.get('x-content-type-options')!r}")
        server = r.headers.get("server", "")
        record("header Server não vaza o servidor ASGI interno (uvicorn/etc.)",
               server.lower() not in ("uvicorn", "starlette"), f"server={server!r}")


async def check_admin_routes_reject(client: httpx.AsyncClient) -> None:
    r = await client.get("/admin/routes")
    record("rota admin sem secret é rejeitada (401)",
           r.status_code == 401, f"status={r.status_code}")
    r2 = await client.get("/admin/routes", headers={"X-Admin-Secret": "chave-errada-de-proposito"})
    record("rota admin com secret errado é rejeitada (401, sem 500/crash)",
           r2.status_code == 401, f"status={r2.status_code}")


async def check_allowlist_blocks_admin_endpoints(client: httpx.AsyncClient, api_key: str) -> None:
    for path, method in [
        ("v1/load_lora_adapter", "POST"),
        ("v1/unload_lora_adapter", "POST"),
        ("v1/tokenize", "POST"),
    ]:
        r = await client.request(
            method, f"/{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            json={}, timeout=15.0,
        )
        record(f"allowlist bloqueia {method} /{path} (404)",
               r.status_code == 404, f"status={r.status_code}")


async def check_models_hides_other_tenants(client: httpx.AsyncClient, api_key: str) -> None:
    r = await client.get("/v1/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=15.0)
    ok = r.status_code == 200
    leaked: list[str] = []
    if ok:
        try:
            data = r.json().get("data", [])
            leaked = [m.get("id") for m in data if str(m.get("id", "")).startswith("acct-")]
        except Exception:
            ok = False
    record("/v1/models não lista adapters acct-* de outros tenants",
           ok and not leaked, f"status={r.status_code} leaked={leaked}")


async def check_model_pinning(client: httpx.AsyncClient, api_key: str) -> None:
    """Manda um "model" forjado (nome de adapter de outro tenant) — se o
    pinning server-side estiver ativo, o gateway ignora e usa o modelo
    travado da própria chave; a resposta vem 200 normalmente. Se o pinning
    tivesse regredido, um account base receberia esse valor sem tradução e
    o vLLM devolveria erro de "modelo não encontrado"."""
    payload = {
        "model": "acct-00000000-0000-0000-0000-000000000000",
        "messages": [{"role": "user", "content": "Responda apenas com a palavra: oi"}],
        "max_tokens": 20,
        "stream": False,
    }
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload, timeout=60.0,
    )
    record("model pinning ignora 'model' forjado no body (200, não erro de modelo)",
           r.status_code == 200, f"status={r.status_code} body={r.text[:200]!r}")


async def check_body_size_limit(client: httpx.AsyncClient, api_key: str) -> None:
    huge = "A" * 2_000_000  # > MAX_BODY_BYTES (default 1MB)
    payload = {"model": "x", "messages": [{"role": "user", "content": huge}]}
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload, timeout=30.0,
    )
    record("corpo acima do limite é rejeitado (413)",
           r.status_code == 413, f"status={r.status_code}")


async def check_message_count_limit(client: httpx.AsyncClient, api_key: str) -> None:
    messages = [{"role": "user", "content": "oi"}] * 300  # > MAX_MESSAGES (default 200)
    payload = {"model": "x", "messages": messages}
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload, timeout=30.0,
    )
    record("número de mensagens acima do limite é rejeitado (400)",
           r.status_code == 400, f"status={r.status_code}")


async def check_anthropic_messages(client: httpx.AsyncClient, api_key: str) -> None:
    """Claude Code fala só essa API — confirma que /v1/messages traduz
    pro formato Anthropic (type=message, content=blocks), autenticando via
    x-api-key (um dos dois headers que o Claude Code usa)."""
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "Responda apenas com a palavra: oi"}],
        "stream": False,
    }
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        json=payload, timeout=60.0,
    )
    ok = r.status_code == 200
    shape_ok = False
    if ok:
        try:
            body = r.json()
            shape_ok = body.get("type") == "message" and isinstance(body.get("content"), list)
        except Exception:
            shape_ok = False
    record("/v1/messages (Claude Code) responde no formato Anthropic",
           ok and shape_ok, f"status={r.status_code} body={r.text[:200]!r}")


async def check_responses_reachable(client: httpx.AsyncClient, api_key: str) -> None:
    """Codex CLI só fala Responses API — confirma que a allowlist permite
    o path (não é sobre tool-calling funcionar, só sobre não ser 404)."""
    payload = {
        "model": "x",
        "input": [{"role": "user", "content": "Responda apenas com a palavra: oi"}],
        "max_output_tokens": 20,
        "stream": False,
    }
    r = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload, timeout=60.0,
    )
    record("/v1/responses (Codex) alcançável (não é 404 da allowlist)",
           r.status_code != 404, f"status={r.status_code} body={r.text[:200]!r}")


# ---------- checks opcionais (custam GPU de verdade — --include-burst) ----------


async def check_rate_limit(client: httpx.AsyncClient, api_key: str, burst: int) -> None:
    statuses: list = []

    async def one():
        try:
            r = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "x", "messages": [{"role": "user", "content": "oi"}],
                      "max_tokens": 5, "stream": False},
                timeout=httpx.Timeout(5.0, connect=5.0),
            )
            statuses.append(r.status_code)
        except httpx.TimeoutException:
            # não foi barrado a tempo — seguiu pra inferência real, que
            # pode legitimamente passar de 5s; não é falha do check
            statuses.append("seguiu_para_inferencia")

    await asyncio.gather(*[one() for _ in range(burst)])
    counts = {str(s): statuses.count(s) for s in set(statuses)}
    record(f"rate limit dispara 429 sob rajada de {burst} requests",
           429 in statuses, f"status_counts={counts}")


async def check_concurrency_limit(client: httpx.AsyncClient, api_key: str, burst: int) -> None:
    """Concorrência não usa mais teto fixo por chave — é elástica por máquina
    (check_concurrency em main.py): uma chave sozinha no pod pode legitimamente
    ocupar quase toda a capacidade, então uma rajada de UMA chave não tem mais
    um número de 429 esperado (depende de machines.max_concurrent_seqs, que
    este script não enxerga sem credencial de admin). O que dá pra verificar
    sem essa informação: SE algum 429 aparecer, o motivo tem que ser o teto
    elástico de capacidade (mensagem nova) — nunca a mensagem do antigo teto
    fixo por chave, o que indicaria deploy desatualizado (imagem antiga)."""
    statuses: list = []
    details: list[str] = []

    async def one():
        try:
            r = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "x", "messages": [{"role": "user", "content": "oi"}],
                      "max_tokens": 5, "stream": False},
                timeout=httpx.Timeout(5.0, connect=5.0),
            )
            statuses.append(r.status_code)
            if r.status_code == 429:
                try:
                    details.append(r.json().get("detail", ""))
                except Exception:
                    details.append("")
        except httpx.TimeoutException:
            statuses.append("seguiu_para_inferencia")

    await asyncio.gather(*[one() for _ in range(burst)])
    counts = {str(s): statuses.count(s) for s in set(statuses)}
    stale_static_cap = any("requisições simultâneas excedido" in d for d in details)
    record(
        f"concorrência sob rajada de {burst} requests (elástica por máquina, sem teto fixo por chave)",
        not stale_static_cap,
        f"status_counts={counts}" + (
            " — ATENÇÃO: mensagem do antigo teto fixo por chave apareceu, deploy pode estar desatualizado"
            if stale_static_cap else ""
        ),
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True, help="URL do gateway")
    parser.add_argument("--api-key", required=True, help="chave HEX de uma conta/stack de TESTE")
    parser.add_argument(
        "--include-burst", action="store_true",
        help="inclui os checks de rate-limit/concorrência (custo real de GPU — ver docstring)",
    )
    parser.add_argument("--burst-size", type=int, default=70,
                         help="requests simultâneas nos checks de rajada (default 70, > RATE_LIMIT_RPM default de 60)")
    parser.add_argument("--concurrency-burst-size", type=int, default=15,
                         help="requests simultâneas no check de concorrência (default 15 — "
                              "não há mais teto fixo esperado, ver docstring de check_concurrency_limit)")
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/")) as client:
        await check_health_and_headers(client)
        await check_admin_routes_reject(client)
        await check_allowlist_blocks_admin_endpoints(client, args.api_key)
        await check_models_hides_other_tenants(client, args.api_key)
        await check_model_pinning(client, args.api_key)
        await check_body_size_limit(client, args.api_key)
        await check_message_count_limit(client, args.api_key)
        await check_anthropic_messages(client, args.api_key)
        await check_responses_reachable(client, args.api_key)

        if args.include_burst:
            print("\n--- checks de rajada (custo real de GPU) ---", flush=True)
            await check_rate_limit(client, args.api_key, args.burst_size)
            await check_concurrency_limit(client, args.api_key, args.concurrency_burst_size)
        else:
            print("\n(--include-burst não usado: pulando rate-limit/concorrência)", flush=True)

    passed = sum(1 for _, p, _ in RESULTS if p)
    total = len(RESULTS)
    print(f"\n=== {passed}/{total} checks passaram ===", flush=True)
    if passed < total:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
