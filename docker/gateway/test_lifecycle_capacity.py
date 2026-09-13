"""Reconciliação e reposição proativa do LifecycleManager — sem fastapi/rede.
Rodar de docker/gateway/:

    python3 -m pytest test_lifecycle_capacity.py
"""

import asyncio
from datetime import datetime, timedelta, timezone

from lifecycle import LifecycleManager


class FakeSupa:
    def __init__(self, machines, running=None, stopped=None, products=None):
        self.machines = machines
        self.status_writes = []
        self.running = running or []
        self.stopped = stopped or []
        self.products = products or []

    async def list_machines_with_pod(self):
        return list(self.machines)

    async def set_machine_status(self, machine_id, status, expected=None):
        self.status_writes.append((machine_id, status))
        return True

    async def touch_machine_activity(self, machine_id):
        return None

    async def list_distinct_products(self):
        return list(self.products)

    async def list_running_machines_for_plan(self, plan, category="llm"):
        return list(self.running)

    async def list_stopped_machines_for_plan(self, plan, category="llm"):
        return list(self.stopped)


class FakeRunpod:
    def __init__(self, pods):
        self.pods = pods

    async def list_pods(self):
        return list(self.pods)


def manager(supa, runpod=None, **kwargs):
    return LifecycleManager(
        store=None,
        supa=supa,
        call_agent=None,
        in_flight={},
        idle_unload_minutes=0,
        drain_timeout_s=0,
        lora_load_timeout_s=0,
        runpod=runpod,
        **kwargs,
    )


def iso(delta_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_s)).isoformat()


# ---------- reconcile: pod recém-criado que reporta EXITED ----------


def test_creating_com_exited_recente_nao_e_rebaixada():
    # a RunPod devolve o pod recém-criado EXITED por instantes; rebaixar a
    # 'stopped' fazia a máquina virar candidata a wake/recriação em cima de um
    # pod que já está subindo (mesma guarda de lib/machines.ts no painel)
    supa = FakeSupa([{"id": "m1", "status": "creating", "runpod_pod_id": "p1",
                      "public_url": "u", "created_at": iso(30), "last_activity_at": iso(30)}])
    mgr = manager(supa, FakeRunpod([{"id": "p1", "desiredStatus": "EXITED"}]), creating_grace_s=900)

    changed = asyncio.run(mgr.reconcile_statuses_once())

    assert changed == []
    assert supa.status_writes == []


def test_creating_com_exited_alem_do_prazo_vira_stopped():
    # passado o prazo, o pod não subiu mesmo: volta o comportamento antigo,
    # e o wake sob demanda sabe religar uma 'stopped'
    supa = FakeSupa([{"id": "m1", "status": "creating", "runpod_pod_id": "p1",
                      "public_url": "u", "created_at": iso(2000), "last_activity_at": iso(2000)}])
    mgr = manager(supa, FakeRunpod([{"id": "p1", "desiredStatus": "EXITED"}]), creating_grace_s=900)

    changed = asyncio.run(mgr.reconcile_statuses_once())

    assert changed == [("m1", "stopped")]


def test_running_com_exited_continua_virando_stopped():
    # a guarda é só para 'creating' — máquina pausada pelo console segue
    # sendo reconciliada como antes
    supa = FakeSupa([{"id": "m1", "status": "running", "runpod_pod_id": "p1",
                      "public_url": "u", "created_at": iso(30), "last_activity_at": iso(30)}])
    mgr = manager(supa, FakeRunpod([{"id": "p1", "desiredStatus": "EXITED"}]), creating_grace_s=900)

    changed = asyncio.run(mgr.reconcile_statuses_once())

    assert changed == [("m1", "stopped")]


# ---------- reposição proativa: conta do pool, não a conta LoRA ----------


def test_reposicao_usa_a_vaga_do_pool_quando_injetada():
    # máquina Go com 18/18 stacks base: a conta LoRA (rotas) diria "8 livres"
    # e o watermark nunca dispararia; a conta do pool diz 0 e provisiona
    supa = FakeSupa([], running=[{"id": "m1"}], products=[("Go", "llm")])
    triggered = []

    async def lora_free(m):
        return 8

    async def pool_free(m):
        return 0

    async def provision(plan, reason, category="llm"):
        triggered.append((plan, category))
        return True

    mgr = manager(
        supa,
        machine_free_slots=lora_free,
        pool_free_slots=pool_free,
        try_provision_for_pool=provision,
        pool_watermark_slots=5,
    )

    result = asyncio.run(mgr.ensure_capacity_once())

    assert result == ["Go/llm"]
    assert triggered == [("Go", "llm")]


def test_reposicao_cai_na_conta_lora_sem_pool_free_slots():
    supa = FakeSupa([], running=[{"id": "m1"}], products=[("Go", "llm")])
    triggered = []

    async def lora_free(m):
        return 8

    async def provision(plan, reason, category="llm"):
        triggered.append(plan)
        return True

    mgr = manager(
        supa, machine_free_slots=lora_free, try_provision_for_pool=provision,
        pool_watermark_slots=5,
    )

    assert asyncio.run(mgr.ensure_capacity_once()) == []
    assert triggered == []
