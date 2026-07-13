#!/usr/bin/env python3
"""
Teste de migração sem perda: N workers fazem chat completions em streaming
contínuo contra o GATEWAY enquanto uma migração acontece no meio. Ao final,
assere que nenhuma request falhou, nenhum stream foi truncado (todos
terminaram com data: [DONE]) e que a rota aponta para o destino.

Uso:
  python3 scripts/test-migration.py \
    --gateway-url http://localhost:8080 \
    --gateway-admin-secret <GATEWAY_ADMIN_SECRET> \
    --api-key <chave HEX da conta> \
    --account-id <uuid da conta> \
    --target-machine-id <uuid da máquina destino> \
    [--workers 4] [--duration 60]

Requer: pip install httpx
"""

import argparse
import asyncio
import json
import sys
import time

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--gateway-url", required=True)
parser.add_argument("--gateway-admin-secret", required=True)
parser.add_argument("--api-key", required=True)
parser.add_argument("--account-id", required=True)
parser.add_argument("--target-machine-id", required=True)
parser.add_argument("--workers", type=int, default=4)
parser.add_argument("--duration", type=int, default=60)
args = parser.parse_args()

GATEWAY = args.gateway_url.rstrip("/")

stats = {"ok": 0, "errors": [], "truncated": 0}
stop = False


async def worker(wid: int):
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        while not stop:
            got_done = False
            status = None
            try:
                async with client.stream(
                    "POST",
                    f"{GATEWAY}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {args.api_key}"},
                    json={
                        "model": "placeholder",  # o gateway reescreve para acct-<id>
                        "messages": [{"role": "user", "content": f"Conte até 30, worker {wid}."}],
                        "max_tokens": 120,
                        "stream": True,
                    },
                ) as resp:
                    status = resp.status_code
                    async for line in resp.aiter_lines():
                        if line.strip() == "data: [DONE]":
                            got_done = True
            except Exception as e:
                stats["errors"].append(f"worker {wid}: {type(e).__name__}: {e}")
                continue
            if status != 200:
                stats["errors"].append(f"worker {wid}: HTTP {status}")
            elif not got_done:
                stats["truncated"] += 1
                stats["errors"].append(f"worker {wid}: stream sem [DONE] (truncado)")
            else:
                stats["ok"] += 1


async def main():
    global stop
    tasks = [asyncio.create_task(worker(i)) for i in range(args.workers)]

    # deixa o tráfego estabilizar, migra no meio, segue o tráfego depois
    await asyncio.sleep(min(10, args.duration / 4))
    print(f"[{time.strftime('%H:%M:%S')}] disparando migração para {args.target_machine_id}…")
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0)) as client:
        r = await client.post(
            f"{GATEWAY}/admin/migrate",
            headers={"X-Admin-Secret": args.gateway_admin_secret},
            json={"account_id": args.account_id, "target_machine_id": args.target_machine_id},
        )
        print(f"[{time.strftime('%H:%M:%S')}] /admin/migrate → {r.status_code}: {r.text}")
        migration = r.json() if r.status_code == 200 else None

    await asyncio.sleep(args.duration)
    stop = True
    await asyncio.gather(*tasks)

    print(f"\nresultado: {stats['ok']} ok · {stats['truncated']} truncado(s) · {len(stats['errors'])} erro(s)")
    for e in stats["errors"][:10]:
        print(f"  - {e}")

    failed = False
    if stats["errors"]:
        print("\nFALHOU: houve erros/streams truncados durante a migração")
        failed = True
    if not migration or migration.get("to") != args.target_machine_id:
        print("\nFALHOU: migração não confirmou o destino")
        failed = True
    if stats["ok"] == 0:
        print("\nFALHOU: nenhuma request completou (teste inválido)")
        failed = True
    if failed:
        sys.exit(1)
    print("OK: nenhuma request perdida durante a migração.")


asyncio.run(main())
