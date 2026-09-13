"""Utilitários do fluxo de recuperação de máquina (auto-wake / recriação /
provisionamento) e das tasks de background do gateway. Isolados de main.py para
serem testáveis sem fastapi nem env vars — mesma disciplina de context_budget.py
e usage_class.py.

Rodar de docker/gateway/:  python3 -m pytest test_recovery.py
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

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


def template_allows_automatic_creation(machine: dict) -> bool:
    """A máquina pode ser recriada automaticamente pelo gateway?

    `templates` é o relacionamento embutido por SupaClient.get_machine. Sem
    template/flags (máquina legada), preserva o comportamento anterior. Um
    template desabilitado ou de teste só pode ser tratado manualmente.
    """
    template = machine.get("templates")
    if not isinstance(template, dict):
        return True
    return template.get("is_enabled", True) is not False and not template.get(
        "is_test", False
    )


def machine_was_lost(machine: dict) -> bool:
    """A máquina SUMIU (merece recriação), ou foi apagada de propósito?

    `status = 'terminated'` hoje carrega dois eventos opostos com o mesmo nome:

      - **o pod sumiu do RunPod** — reconcile_statuses_once promove a
        'terminated' quando o id some da listagem (host reclamou a instância,
        pod deletado pelo console), e o painel faz o mesmo ao levar 404. Nos
        dois o `runpod_pod_id` fica intacto, porque ninguém pediu que a máquina
        deixasse de existir.
      - **o usuário clicou em apagar** — terminateMachine (lib/actions.ts) chama
        deletePod e ZERA runpod_pod_id/public_url junto com o status.

    A distinção é o que separa "restaurar o que caiu" de "ressuscitar o que
    alguém acabou de apagar" — e a segunda custa uma GPU por hora que ninguém
    pediu. Sem ela, uma requisição atrasada de um cliente cujo Claude Code
    ainda estava aberto religaria a máquina minutos depois do delete.

    'error' entra sem exigir pod_id: é o status de um pod que existe e falhou,
    não de um que foi removido.

    Fail-CLOSED por omissão: sem runpod_pod_id não recria. Uma máquina legada
    sem o campo preenchido perde a recriação automática (recuperável à mão); o
    inverso criaria pod sozinho, que é o erro caro.
    """
    status = machine.get("status")
    if status == "error":
        return True
    return status == "terminated" and bool(machine.get("runpod_pod_id"))


# ---------- Máquina presa em 'creating' ----------


def machine_boot_stalled(machine: dict, max_age_s: float) -> bool:
    """A máquina está em 'creating' há mais tempo do que qualquer boot real
    levaria? Segunda linha de defesa do 503 eterno.

    wake_some_machine_for_plan devolve 'waking' se existe QUALQUER máquina
    'creating' no plano — guarda necessária pra não religar/provisionar em cima
    de um boot em andamento, mas que confia no status sem olhar a idade. Se algo
    prende uma máquina em 'creating' (o caso conhecido era o reconcile pulando
    pod ausente sem prazo, corrigido em lifecycle.reconcile_statuses_once), essa
    casca barra o auto-wake E o provisionamento do plano inteiro, pra sempre.

    Aqui o wake ignora a 'creating' velha demais e segue pra cascata. O reconcile
    continua sendo quem conserta o STATUS; este filtro só impede que uma máquina
    presa, por qualquer motivo futuro, derrube a disponibilidade do plano.

    Fail-CLOSED por omissão: sem timestamp legível a máquina NÃO é considerada
    presa (continua segurando a cascata), que é o comportamento de antes.
    """
    if max_age_s <= 0:
        return False
    raw = machine.get("last_activity_at") or machine.get("created_at")
    if not raw:
        return False
    try:
        since = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - since > timedelta(seconds=max_age_s)
