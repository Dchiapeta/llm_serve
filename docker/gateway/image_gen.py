"""Persistência das imagens geradas: o que gravar, onde e com que metadados.

O pod de difusão devolve `b64_json` e nada mais — a imagem existe no corpo da
resposta e desaparece. Este módulo transforma essa resposta no par
(bytes do arquivo, linha do banco) que o gateway grava antes de responder ao
cliente, para que a pergunta "quem gerou esta imagem" tenha resposta.

Módulo puro (sem httpx, sem fastapi, sem I/O), como image_proxy.py e
usage_class.py: aqui mora a DECISÃO — que formato é o arquivo, onde ele vai no
bucket, o que cada coluna recebe — e o I/O fica em main.py/supa.py. É o que
permite testar a parte que erra (path, metadados, formato) sem subir nada.

## De onde vêm os metadados

De duas fontes, nesta ordem de precedência:

  1. o bloco `meta` que o POD devolve, quando devolve. É a fonte AUTORITATIVA:
     são os valores efetivamente usados na geração, incluindo a seed que o pod
     sorteou quando o cliente não mandou nenhuma.
  2. o corpo da REQUISIÇÃO, como fallback.

A ordem não é uma preferência estética. Em `/v1/images/edits` o corpo é
multipart repassado em streaming e o gateway nunca o parseia — sem o bloco do
pod não há metadado nenhum a gravar. E mesmo em `generations`, o corpo só tem o
que o CLIENTE mandou: com `seed` ausente, gravar o que veio no corpo significa
gravar `None` para uma imagem que tem, sim, uma seed — e o registro deixa de
reproduzir a imagem que promete descrever.

Um pod de versão antiga (sem o bloco `meta`) continua funcionando: cai no
fallback e grava o que dá, com os campos que só ele saberia ficando nulos.
"""

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Faixa da coluna `seed` (numeric(20,0) com CHECK, migration 0059). A seed do
# pipeline é um inteiro de 64 bits sem sinal; um valor fora disso é sinal de
# resposta corrompida, e gravá-lo faria o insert estourar DEPOIS de o upload já
# ter acontecido — o pior momento possível. Filtrar aqui degrada para "seed
# nula" em vez de derrubar a persistência inteira por um campo acessório.
SEED_MAX = 2**64 - 1

# Assinaturas dos formatos que o pod pode emitir (IMAGE_OUTPUT_FORMAT é env em
# docker/image/server.py: png por default, mas jpeg e webp são configuráveis).
# Detectar pelo conteúdo, e não pela env do pod, porque o gateway não a lê — e
# um content_type errado no bucket faz o navegador baixar o arquivo em vez de
# exibi-lo, que é justamente o que a galeria precisa.
_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
]

# Fallback de formato desconhecido. `application/octet-stream` e não "image/png"
# chutado: um content_type mentiroso é pior que um genérico — o genérico faz o
# cliente baixar, o mentiroso faz o decode falhar sem explicação.
_UNKNOWN = ("application/octet-stream", "bin")


class MalformedImageResponse(ValueError):
    """A resposta do pod não tem o formato esperado (`data[].b64_json`).

    Levantada em vez de degradar em silêncio: se o formato mudou, gravar zero
    imagens sem avisar transformaria uma quebra de contrato numa perda de dados
    lenta, que só apareceria quando alguém procurasse a imagem semanas depois.
    """


@dataclass
class PendingImage:
    """Uma imagem pronta para gravar: os bytes e a linha que os descreve."""

    storage_path: str
    content_type: str
    data: bytes
    row: dict[str, Any] = field(default_factory=dict)


def detect_content_type(raw: bytes) -> tuple[str, str]:
    """(content_type, extensão) a partir dos primeiros bytes do arquivo."""
    for magic, content_type, ext in _MAGIC:
        if raw.startswith(magic):
            return content_type, ext
    # WEBP é "RIFF" + 4 bytes de tamanho + "WEBP": não dá pra casar com um
    # startswith simples como os outros dois.
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp", "webp"
    return _UNKNOWN


def storage_path(
    *, stack_id: str, batch_id: str, index: int, ext: str, now: datetime
) -> str:
    """Caminho do objeto DENTRO do bucket: '{stack}/{data}/{batch}-{i}.{ext}'.

    Relativo ao bucket, sem prefixo "images/" — o bucket já se chama images, e
    repetir o nome criaria uma pasta "images" dentro de cada path.

    O prefixo por STACK (não por conta) é a convenção do bucket "knowledge", e
    existe pelo mesmo motivo: duas stacks da mesma conta não podem se
    sobrescrever. A data no meio não é usada por nenhuma query — é para o humano
    que abre o bucket no dashboard e precisa achar "as imagens de ontem" sem
    consultar o banco.
    """
    return f"{stack_id}/{now:%Y-%m-%d}/{batch_id}-{index}.{ext}"


def expires_at(now: datetime, retention_days: int) -> datetime:
    """Quando o ARQUIVO desta linha pode ser apagado pelo reaper."""
    return now + timedelta(days=retention_days)


def _as_int(value: Any) -> int | None:
    """int ou None, sem levantar. Metadado é acessório: um campo com lixo vira
    nulo e a imagem é gravada assim mesmo — perder a linha inteira porque o
    `steps` veio como string seria trocar um dado ausente por um dado perdido.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_seed(value: Any) -> int | None:
    """Seed dentro da faixa da coluna, ou None. Ver SEED_MAX."""
    seed = _as_int(value)
    if seed is None or seed < 0 or seed > SEED_MAX:
        return None
    return seed


def parse_size(raw: Any) -> tuple[int | None, int | None]:
    """'1024x1536' -> (1024, 1536). Qualquer outra coisa -> (None, None)."""
    if not isinstance(raw, str) or "x" not in raw:
        return None, None
    left, _, right = raw.partition("x")
    return _as_int(left.strip()), _as_int(right.strip())


def request_meta(body_json: Any) -> dict[str, Any]:
    """Metadados extraídos do CORPO da requisição (fallback, ver docstring do
    módulo). Só serve a /v1/images/generations, que é JSON: o corpo do edits é
    multipart repassado em streaming e nunca chega a ser parseado aqui."""
    if not isinstance(body_json, dict):
        return {}
    width, height = parse_size(body_json.get("size"))
    prompt = body_json.get("prompt")
    return {
        "prompt": prompt if isinstance(prompt, str) else None,
        "width": width,
        "height": height,
        "steps": _as_int(body_json.get("steps")),
        "guidance_scale": _as_float(body_json.get("guidance_scale")),
        "seed": _as_seed(body_json.get("seed")),
    }


def _pod_meta(payload: dict) -> dict[str, Any]:
    """Metadados do bloco `meta` da resposta do pod, se houver."""
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return {}
    prompt = meta.get("prompt")
    return {
        "prompt": prompt if isinstance(prompt, str) else None,
        "width": _as_int(meta.get("width")),
        "height": _as_int(meta.get("height")),
        "steps": _as_int(meta.get("steps")),
        "guidance_scale": _as_float(meta.get("guidance_scale")),
        "seed": _as_seed(meta.get("seed")),
    }


def _merge(pod: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Campo a campo, o do pod ganha quando não é None (ver docstring)."""
    return {k: (pod.get(k) if pod.get(k) is not None else fallback.get(k))
            for k in ("prompt", "width", "height", "steps", "guidance_scale", "seed")}


def plan_persistence(
    payload: Any,
    *,
    batch_id: str,
    account_id: str,
    stack_id: str,
    api_key_id: str,
    machine_id: str,
    path: str,
    model: str | None,
    fallback_meta: dict[str, Any] | None = None,
    now: datetime | None = None,
    retention_days: int,
) -> list[PendingImage]:
    """Resposta do pod -> lista de imagens a gravar (bytes + linha).

    Levanta MalformedImageResponse se `data` não for uma lista de itens com
    `b64_json` decodificável. Um item isolado que falhe o decode derruba o lote
    inteiro de propósito: a alternativa seria responder 200 com uma das imagens
    silenciosamente não gravada, e o cliente não teria como saber qual.
    """
    if not isinstance(payload, dict):
        raise MalformedImageResponse("resposta do pod não é um objeto JSON")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise MalformedImageResponse("resposta do pod sem `data`")

    now = now or datetime.now(timezone.utc)
    meta = _merge(_pod_meta(payload), fallback_meta or {})
    expiry = expires_at(now, retention_days)

    pending: list[PendingImage] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
            raise MalformedImageResponse(f"item {index} sem `b64_json`")
        try:
            raw = base64.b64decode(item["b64_json"], validate=True)
        except (binascii.Error, ValueError) as e:
            raise MalformedImageResponse(f"item {index} com base64 inválido: {e}")
        if not raw:
            raise MalformedImageResponse(f"item {index} decodificou para 0 bytes")

        content_type, ext = detect_content_type(raw)
        key = storage_path(
            stack_id=stack_id, batch_id=batch_id, index=index, ext=ext, now=now
        )
        # a seed é POR IMAGEM quando o pod a devolve nos itens: com n>1 cada
        # imagem tem a sua, e um valor de topo descreveria só a primeira.
        seed = _as_seed(item.get("seed"))
        pending.append(
            PendingImage(
                storage_path=key,
                content_type=content_type,
                data=raw,
                row={
                    "batch_id": batch_id,
                    "image_index": index,
                    "account_id": account_id,
                    "stack_id": stack_id,
                    "api_key_id": api_key_id,
                    "machine_id": machine_id,
                    "path": path,
                    "model": model,
                    "prompt": meta["prompt"],
                    "width": meta["width"],
                    "height": meta["height"],
                    "steps": meta["steps"],
                    "guidance_scale": meta["guidance_scale"],
                    "seed": seed if seed is not None else meta["seed"],
                    "storage_path": key,
                    "content_type": content_type,
                    "bytes": len(raw),
                    "expires_at": expiry.isoformat(),
                },
            )
        )
    return pending
