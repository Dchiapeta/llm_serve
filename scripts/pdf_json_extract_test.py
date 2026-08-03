#!/usr/bin/env python3
"""
Teste de integração do endpoint /v1/documents/extract (extração estruturada de
documento: PDF → JSON). Manda o PDF em multipart pro GATEWAY — que é quem faz
a extração/OCR e chama o modelo — e valida a resposta contra o mesmo schema
enviado na request.

Não extrai texto localmente de propósito: a extração é responsabilidade do
gateway (docker/gateway/document_extract.py). Se este script fizesse o parsing,
não estaria testando o produto, e sim uma reimplementação dele.

Pré-requisito: guided decoding ligado no template do plano
(supabase/migrations/0047_guided_decoding_pdf_json.sql) E a máquina RECRIADA
depois da migration — pod em execução carrega o env do momento em que foi
criado. Sem isso o endpoint responde 502 com o texto cru do modelo, que é
exatamente o sintoma dessa configuração faltando.

Uso:
  python3 scripts/pdf_json_extract_test.py \
    --base-url https://llmserve-docker.up.railway.app \
    --api-key <chave HEX da stack de teste> \
    --doc nota_fiscal.pdf:schemas/nota_fiscal.json \
    --doc contrato.pdf:schemas/contrato.json \
    --repeat 5 \
    --out pdf_json_results.json

Requer: pip install httpx jsonschema
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
import jsonschema


async def run_one(
    client: httpx.AsyncClient, args, pdf_path: Path, pdf_bytes: bytes, schema: dict
) -> dict:
    t0 = time.monotonic()
    result = {
        "doc": pdf_path.name, "ok": False, "error": None, "total_s": None,
        "pages": None, "ocr_used": None, "usage": None, "output": None,
    }
    try:
        resp = await client.post(
            "/v1/documents/extract",
            headers={"Authorization": f"Bearer {args.api_key}"},
            files={"file": (pdf_path.name, pdf_bytes, "application/pdf")},
            data={"schema": json.dumps(schema)},
            timeout=httpx.Timeout(args.timeout, connect=15.0),
        )
        result["total_s"] = round(time.monotonic() - t0, 2)
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:500]}"
            return result
        body = resp.json()
        result.update(
            pages=body.get("pages"), ocr_used=body.get("ocr_used"),
            usage=body.get("usage"), output=body.get("data"),
        )
        # o gateway já valida contra o schema antes de responder; revalidar aqui
        # é o controle independente — se um dia essa checagem for afrouxada lá,
        # este teste ainda pega.
        jsonschema.validate(body.get("data"), schema)
        result["ok"] = True
    except jsonschema.ValidationError as e:
        result["error"] = f"resposta não aderente ao schema: {e.message}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    if result["total_s"] is None:
        result["total_s"] = round(time.monotonic() - t0, 2)
    return result


async def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True, help="URL do gateway")
    parser.add_argument("--api-key", required=True, help="Chave HEX da stack de teste")
    parser.add_argument(
        "--doc", action="append", required=True, dest="docs",
        help="par pdf:schema.json — pode repetir pra testar vários documentos",
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0,
        help="Teto de espera por documento. Maior que o DOCUMENT_UPSTREAM_TIMEOUT_S "
        "do gateway (240s default) de propósito: o cliente precisa sobreviver ao "
        "timeout do servidor pra ver o erro dele, não o seu.",
    )
    parser.add_argument(
        "--repeat", type=int, default=1,
        help="Repetições por documento — é o que mede CONSISTÊNCIA. Uma passada "
        "só não distingue 'funciona' de 'funcionou desta vez'.",
    )
    parser.add_argument("--out", default="pdf_json_results.json")
    args = parser.parse_args()

    pairs = []
    for d in args.docs:
        pdf_str, schema_str = d.split(":", 1)
        pairs.append((Path(pdf_str), Path(schema_str)))

    all_results = []
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/")) as client:
        for pdf_path, schema_path in pairs:
            pdf_bytes = pdf_path.read_bytes()
            schema = json.loads(schema_path.read_text())
            print(f"[{pdf_path.name}] {len(pdf_bytes) // 1024} KB", flush=True)
            for i in range(args.repeat):
                r = await run_one(client, args, pdf_path, pdf_bytes, schema)
                all_results.append(r)
                extra = f" pages={r['pages']} ocr={r['ocr_used']}" if r["ok"] else ""
                status = "OK" if r["ok"] else f"FALHOU: {r['error']}"
                print(f"  #{i + 1} {r['total_s']}s{extra} — {status}", flush=True)

    ok = sum(1 for r in all_results if r["ok"])
    print(f"\n=== {ok}/{len(all_results)} JSON válidos e aderentes ao schema ===")
    with open(args.out, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"Resultados salvos em {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
