"""Retenção das imagens geradas: apaga o ARQUIVO aos 30 dias, mantém o registro.

O bucket cresce com tudo que o plano Image gera e nada sai sozinho — uma imagem
1024×1024 pesa ~1,5 MB, e sem expiração o custo de storage é monotônico. Este é
o primeiro job de retenção do projeto; até aqui nenhuma tabela tinha prazo.

## O que expira e o que fica

Expira o arquivo no bucket e o `prompt` da linha. Fica o resto do registro —
quem gerou, quando, por qual stack e chave, que tamanho tinha. É a divisão entre
o ARTEFATO, que tem custo e prazo, e a AUDITORIA, que responde "quem gerou esta
imagem" muito depois de a imagem ter sumido.

O prompt sai junto com o arquivo por minimização: é texto livre do cliente, pode
conter qualquer coisa, e sem a imagem não reproduz mais nada.

## Ordem: Storage ANTES do banco

Sempre. `storage_path` é o único ponteiro para o arquivo; marcar
`file_deleted_at` primeiro e falhar no delete deixaria um objeto pago no bucket
que nenhum ciclo futuro encontraria — a linha já teria saído da fila. É a mesma
ordem que removeStoragePrefix documenta em lib/actions.ts.

## Falhar para o lado de repetir

Todo o desenho erra para o lado de tentar de novo, nunca para o de marcar como
feito o que não foi:

  - 404 no delete conta como SUCESSO (o objeto já não está lá — é o estado
    desejado; tratá-lo como falha prenderia o lote para sempre);
  - falha real de delete NÃO marca a linha, que volta no ciclo seguinte;
  - o UPDATE é condicionado a `file_deleted_at is null`, então dois ciclos
    concorrentes não sobrescrevem o timestamp um do outro.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from recovery import lock_active

logger = logging.getLogger("gateway.image_retention")

# Quantas linhas por ciclo. O delete do Storage aceita uma lista de prefixos, e
# 200 mantém o corpo da chamada pequeno e o ciclo curto. Um acúmulo maior drena
# em vários ciclos, em ordem de expiração (list_expired_images ordena por
# expires_at), em vez de tentar uma limpeza gigante que falha inteira.
DEFAULT_BATCH = 200

# Trava com TTL para o loop e o POST /admin/reap-images não rodarem em cima um
# do outro. O TTL é folgadamente maior que um ciclo normal (dois round-trips ao
# Supabase) — ver lock_active: entrada mais velha que isto é tratada como
# vazada, e não trava o reaper para sempre se a task morrer antes do finally.
_LOCK_KEY = "image_retention"
LOCK_TTL_S = 300.0
_locks: dict[str, float] = {}


async def reap_expired_images_once(supa, *, batch: int = DEFAULT_BATCH) -> int:
    """Um ciclo de limpeza. Devolve quantas linhas foram fechadas.

    Idempotente: rodar duas vezes seguidas não muda nada além de gastar duas
    consultas. É o que permite expor o disparo manual sem risco.
    """
    if lock_active(_locks, _LOCK_KEY, LOCK_TTL_S):
        logger.info("image retention: ciclo já em andamento, pulando")
        return 0
    _locks[_LOCK_KEY] = time.time()
    try:
        now = datetime.now(timezone.utc)
        rows = await supa.list_expired_images(now.isoformat(), batch)
        if not rows:
            return 0

        # Agrupa por path para uma única chamada de delete, mas guarda o id de
        # cada uma: o UPDATE é por id, e duas linhas nunca compartilham path (o
        # UNIQUE da migration 0059 garante).
        by_path = {r["storage_path"]: r["id"] for r in rows if r.get("storage_path")}
        if not by_path:
            return 0

        deleted_ids: list[str] = []
        try:
            await supa.delete_image_objects(list(by_path))
            deleted_ids = list(by_path.values())
        except Exception as e:
            # Falha do lote inteiro. Não marca nada: o ciclo seguinte tenta de
            # novo, e o custo de repetir um delete é zero (404 conta como
            # sucesso). Marcar aqui trocaria um retry barato por arquivo órfão
            # permanente.
            logger.warning("image retention: delete do lote falhou (%s)", e)
            return 0

        await supa.mark_images_file_deleted(deleted_ids, now.isoformat())
        logger.info("image retention: %d imagens expiradas removidas", len(deleted_ids))
        return len(deleted_ids)
    finally:
        _locks.pop(_LOCK_KEY, None)


async def image_retention_loop(supa, interval_s: float = 3600.0) -> None:
    """Loop de retenção. Mesma forma dos outros loops do gateway (lifecycle.py):
    dorme, chama o `_once`, e engole a exceção — um ciclo que falha é retomado no
    próximo, e nenhum deles pode derrubar o processo que serve o tráfego.

    Intervalo de uma hora: a expiração tem granularidade de dias, então checar
    com mais frequência só gastaria consultas.
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            await reap_expired_images_once(supa)
        except Exception as e:
            logger.warning("image retention: ciclo falhou (%s)", e)


def retention_cutoff(now: datetime, days: int) -> datetime:
    """Data-limite de um período de retenção — usada nos testes e por quem
    precisar calcular o corte sem replicar o sinal do timedelta."""
    return now - timedelta(days=days)
