"""Utilitários do fluxo de recuperação de máquina (auto-wake / recriação /
provisionamento) e das tasks de background do gateway. Isolados de main.py para
serem testáveis sem fastapi nem env vars — mesma disciplina de context_budget.py
e usage_class.py.

Rodar de docker/gateway/:  python3 -m pytest test_recovery.py
"""

import asyncio
import time

# ---------- Tasks fire-and-forget ----------

# O event loop só guarda referência FRACA às tasks: uma task sem referência
# forte pode ser coletada pelo GC no meio de um await (ex.: um POST de 60s ao
# painel), e aí o `finally` que libera uma trava (recreating/provisioning/
# key_sync) NUNCA roda — a trava fica presa até restart e todo request seguinte
# é barrado. Segurar a referência aqui até a task terminar é o padrão canônico
# contra isso.
_background_tasks: "set[asyncio.Task]" = set()


def spawn_tracked(coro) -> "asyncio.Task":
    """create_task que mantém referência forte à task até ela concluir."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ---------- Travas com TTL ----------


def lock_active(lock: dict, key: str, ttl: float) -> bool:
    """True se `key` tem uma trava ativa e ainda dentro do TTL. Uma entrada mais
    velha que o TTL é tratada como VAZADA (a task que deveria liberá-la morreu
    antes do `finally`) — remove e retorna False, deixando o fluxo re-disparar.
    Sem esse TTL, uma trava presa só sairia reiniciando o processo. O TTL de
    cada trava é folgadamente maior que a duração legítima da sua task, então
    uma operação válida em andamento nunca é confundida com vazamento.

    `lock` é um dict {key: timestamp_de_quando_travou}; a marcação é
    `lock[key] = time.time()` e a liberação normal é `lock.pop(key, None)`."""
    ts = lock.get(key)
    if ts is None:
        return False
    if time.time() - ts >= ttl:
        lock.pop(key, None)
        return False
    return True


# ---------- Classificação de erro do RunPod ----------

# Padrões (em minúsculas) que a RunPod devolve quando o host não tem GPU livre
# pra religar um pod pausado — religar é impossível e o chamador precisa recriar
# num host novo. Conservador de propósito: recriar DESTRÓI o pod, então só
# entram frases que inequivocamente significam "host sem GPU livre", nunca falha
# genérica. Antes a checagem era o literal exato "not enough free GPUs"; qualquer
# mudança de caixa/plural/wording da RunPod caía em "failed" e a recriação nunca
# disparava (a máquina ficava em loop de wake fracassado).
_NO_GPU_ERROR_PATTERNS = (
    "not enough free gpu",  # "not enough free GPUs" — mensagem canônica
    "not enough gpu",
    "no gpus available",
    "no gpu available",
    "insufficient gpu",
)


def is_no_gpu_error(exc: object) -> bool:
    """True se o erro do startPod indica host sem GPU livre (→ recriar). Normaliza
    caixa e cobre variações de wording da RunPod (ver _NO_GPU_ERROR_PATTERNS)."""
    msg = str(exc).lower()
    return any(p in msg for p in _NO_GPU_ERROR_PATTERNS)
