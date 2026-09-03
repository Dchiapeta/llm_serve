"""Tetos de corpo e corte incremental de upload das rotas /v1/images/*.

As rotas de geração de imagem não passam pelo catch-all /v1/{path} — elas têm
handler próprio em main.py, registrado antes dele. Isso significa que o
MAX_BODY_BYTES do catch-all não as protege, e que o teto delas precisa ser
DIFERENTE do dele nos dois sentidos:

  - `generations` é texto (prompt + parâmetros). 8 MB ali seria três ordens de
    grandeza acima de qualquer corpo legítimo.
  - `edits` carrega até IMAGE_MAX_REFERENCE_IMAGES arquivos de
    IMAGE_MAX_FILE_SIZE_MB cada (4 × 15 MiB no template atual). 8 MB recusaria
    o caso de uso normal da rota.

## Por que DOIS guards, e não só o middleware

`reject_oversized_upload` (main.py) corta pelo header Content-Length, antes de
qualquer byte ser lido — é o que impede DoS anônimo. Mas Content-Length é
OPCIONAL: numa requisição `Transfer-Encoding: chunked` o header não existe e o
middleware não tem o que comparar. Sem a contagem incremental daqui, esse
caminho ficava sem teto nenhum.

`counting_stream` é a segunda metade: conta os bytes conforme eles passam e
aborta no primeiro chunk que ultrapassa o teto. A memória do gateway fica
limitada ao tamanho de um chunk, não ao do corpo — que é o ponto de repassar o
upload em streaming em vez de acumulá-lo.

Módulo puro (sem fastapi, sem httpx), como recovery.py, cli_policy.py e
content_policy.py: a decisão é função do tamanho, e o I/O fica em main.py.
"""

import os
from typing import AsyncIterator

# Espelham os defaults do pod (docker/image/server.py): IMAGE_MAX_FILE_SIZE_MB
# e IMAGE_MAX_REFERENCE_IMAGES. Ficam aqui como CONSTANTES DE DIMENSIONAMENTO,
# não como validação: quem recusa o 5º arquivo ou o arquivo de 16 MiB é o pod,
# que é quem conhece a própria configuração. O gateway só precisa de um teto de
# corpo grande o bastante pra nunca recusar um pedido que o pod aceitaria.
_REF_IMAGES = 4
_BYTES_PER_REF = 15 * 1024 * 1024

# Folga sobre a soma dos arquivos: cada parte do multipart carrega headers
# (Content-Disposition, Content-Type) mais a boundary, e ainda há os campos de
# texto (prompt, size, steps...). Comparar cru contra 4×15 MiB recusaria um
# upload legítimo exatamente no limite — mesma lógica do MAX_SCHEMA_BYTES + 64 KiB
# que o guard de /v1/documents/extract já usa.
_MULTIPART_OVERHEAD = 1024 * 1024

# `generations` é JSON com prompt e parâmetros escalares. 256 KiB cobre um
# prompt absurdamente longo com folga; acima disso não é caso de uso, é corpo
# malformado ou abuso.
_GENERATION_BYTES = 256 * 1024


def max_generation_bytes() -> int:
    """Teto de corpo de POST /v1/images/generations (JSON)."""
    return _env_int("MAX_IMAGE_GENERATION_BYTES", _GENERATION_BYTES)


def max_edit_bytes() -> int:
    """Teto de corpo de POST /v1/images/edits (multipart com as referências)."""
    return _env_int(
        "MAX_IMAGE_EDIT_BYTES", _REF_IMAGES * _BYTES_PER_REF + _MULTIPART_OVERHEAD
    )


def _env_int(name: str, default: int) -> int:
    """Env numérica com fallback tolerante.

    `or str(default)` e não `os.environ.get(name, str(default))`: uma variável
    DECLARADA e VAZIA (acontece no painel do Railway ao limpar o campo em vez de
    remover a linha) faria int("") levantar no import e o gateway inteiro não
    subiria. Mesmo cuidado do BILLING_GRACE_HOURS em main.py.
    """
    try:
        return int(os.environ.get(name) or str(default))
    except ValueError:
        return default


class UploadTooLarge(Exception):
    """Corpo passou do teto durante o streaming (caminho sem Content-Length).

    Exceção própria, e não HTTPException, por dois motivos. O módulo é puro —
    importar fastapi aqui quebraria o teste que roda sem ele. E, mais
    importante, ela é levantada de DENTRO do async-iterator que o httpx
    consome: o handler precisa distingui-la de uma falha de rede genuína pra
    responder 413 em vez de 503. Ver `unwrap` abaixo.
    """

    def __init__(self, ceiling: int, seen: int):
        self.ceiling = ceiling
        self.seen = seen
        super().__init__(f"corpo excedeu {ceiling} bytes (recebidos ao menos {seen})")


def unwrap(exc: BaseException) -> UploadTooLarge | None:
    """Acha um UploadTooLarge na cadeia de causas, ou None.

    O httpx consome o iterador do corpo dentro do próprio transporte, e uma
    exceção que sobe dali pode chegar ao handler embrulhada num erro de
    transporte (httpx.WriteError e afins) com a original em __cause__. Sem
    desembrulhar, o `except httpx.HTTPError` genérico do handler a capturaria
    primeiro e o cliente receberia 503 — "máquina indisponível" — para um corpo
    que ELE mandou grande demais. O 413 é a única resposta acionável aqui.

    Percorre __cause__ e __context__ com um teto de profundidade: cadeia
    circular de exceções é rara mas possível, e um while sem limite aqui
    penduraria o handler.
    """
    seen: list[int] = []
    current: BaseException | None = exc
    for _ in range(10):
        if current is None or id(current) in seen:
            return None
        if isinstance(current, UploadTooLarge):
            return current
        seen.append(id(current))
        current = current.__cause__ or current.__context__
    return None


async def counting_stream(
    chunks: AsyncIterator[bytes], ceiling: int
) -> AsyncIterator[bytes]:
    """Repassa `chunks` contando os bytes; levanta UploadTooLarge ao estourar.

    O corte é ANTES do yield do chunk que estoura, não depois: repassar o chunk
    e só então abortar mandaria ao pod um corpo multipart truncado, que ele
    gastaria tempo tentando parsear.

    Não acumula nada — o consumidor (httpx) recebe chunk a chunk e a memória do
    gateway fica no tamanho de um chunk. É a diferença entre este caminho e o
    `await request.body()` do catch-all, e o motivo de `edits` poder ter um teto
    de dezenas de MiB num processo que é réplica única e compartilhado por todos
    os tenants.
    """
    seen = 0
    async for chunk in chunks:
        seen += len(chunk)
        if seen > ceiling:
            raise UploadTooLarge(ceiling, seen)
        yield chunk
