#!/usr/bin/env python3
"""
Teste de validação do caso de uso "extração estruturada de documento
(PDF -> JSON)": para cada PDF de entrada, extrai o texto localmente
(pymupdf; cai para OCR via pytesseract se a página não tiver texto embutido
— PDF escaneado), monta uma chamada de chat completion contra o GATEWAY
(nunca direto no pod, ver docs/load-testing-playbook.md) com response_format
json_schema, e valida se a resposta é JSON válido e aderente ao schema.
Mede taxa de sucesso e latência por documento.

Depende de guided decoding ligado no vLLM (--guided-decoding-backend, ver
supabase/migrations/0047_guided_decoding_pdf_json.sql) — sem isso o modelo
pode devolver JSON malformado ocasionalmente, mesmo pedindo via prompt.

Uso:
  python3 scripts/pdf_json_extract_test.py \
    --base-url https://llmserve-docker.up.railway.app \
    --api-key <chave HEX da stack de teste> \
    --model pro-base \
    --doc nota_fiscal.pdf:schemas/nota_fiscal.json \
    --doc contrato.pdf:schemas/contrato.json \
    --repeat 5 \
    --out pdf_json_results.json

Requer: pip install httpx pymupdf pytesseract pillow jsonschema
(pytesseract também precisa do binário `tesseract` instalado no sistema —
só é usado se o PDF não tiver texto embutido extraível)
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
import jsonschema

try:
    import fitz  # pymupdf
except ImportError:
    fitz = None


def extract_text(pdf_path: Path) -> str:
    """Extrai texto de um PDF. Primeiro tenta texto embutido (pymupdf); se
    uma página não tiver texto extraível (PDF escaneado/imagem), cai para
    OCR via pytesseract renderizando a página como imagem."""
    if fitz is None:
        raise RuntimeError("pymupdf não instalado — pip install pymupdf")
    parts = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text = page.get_text().strip()
            if text:
                parts.append(text)
                continue
            import io

            import pytesseract
            from PIL import Image

            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            parts.append(pytesseract.image_to_string(img, lang="por+eng").strip())
    return "\n\n".join(parts)


def build_prompt(doc_text: str, schema: dict) -> str:
    return (
        "Extraia as informações do documento abaixo e devolva SOMENTE um "
        "JSON válido, aderente ao schema fornecido. Não inclua texto fora "
        "do JSON.\n\n"
        f"--- DOCUMENTO ---\n{doc_text}\n--- FIM DO DOCUMENTO ---\n\n"
        f"Schema JSON alvo:\n{json.dumps(schema, ensure_ascii=False)}"
    )


async def run_one(client: httpx.AsyncClient, args, name: str, doc_text: str, schema: dict) -> dict:
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": build_prompt(doc_text, schema)}],
        "max_tokens": args.max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name.replace(".", "_"), "schema": schema},
        },
    }
    t0 = time.monotonic()
    result = {"doc": name, "ok": False, "error": None, "total_s": None, "output": None}
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {args.api_key}"},
            json=payload,
            timeout=httpx.Timeout(args.timeout, connect=15.0),
        )
        result["total_s"] = round(time.monotonic() - t0, 2)
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:300]!r}"
            return result
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        jsonschema.validate(parsed, schema)
        result["output"] = parsed
        result["ok"] = True
    except json.JSONDecodeError as e:
        result["error"] = f"JSON inválido: {e}"
    except jsonschema.ValidationError as e:
        result["error"] = f"Não aderente ao schema: {e.message}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    if result["total_s"] is None:
        result["total_s"] = round(time.monotonic() - t0, 2)
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="URL do gateway (ex: https://llmserve-docker.up.railway.app)")
    parser.add_argument("--api-key", required=True, help="Chave HEX da stack de teste")
    parser.add_argument("--model", required=True, help="served-model-name do template (ex: pro-base)")
    parser.add_argument(
        "--doc", action="append", required=True, dest="docs",
        help="par pdf:schema.json — pode repetir para testar vários documentos",
    )
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--repeat", type=int, default=1, help="repetições por documento, pra medir consistência do schema")
    parser.add_argument("--out", default="pdf_json_results.json")
    args = parser.parse_args()

    pairs = []
    for d in args.docs:
        pdf_str, schema_str = d.split(":", 1)
        pairs.append((Path(pdf_str), Path(schema_str)))

    all_results = []
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/")) as client:
        for pdf_path, schema_path in pairs:
            print(f"[extraindo texto] {pdf_path}", flush=True)
            doc_text = extract_text(pdf_path)
            schema = json.loads(schema_path.read_text())
            for i in range(args.repeat):
                r = await run_one(client, args, pdf_path.name, doc_text, schema)
                all_results.append(r)
                status = "OK" if r["ok"] else f"FALHOU: {r['error']}"
                print(f"  [{pdf_path.name} #{i + 1}] {r['total_s']}s — {status}", flush=True)

    ok = sum(1 for r in all_results if r["ok"])
    total = len(all_results)
    print(f"\n=== {ok}/{total} JSON válidos e aderentes ao schema ===")
    with open(args.out, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"Resultados salvos em {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
