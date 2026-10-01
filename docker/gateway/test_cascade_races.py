"""Corridas da cascata de geração via request (wake → recreate → provision).

    python3 -m pytest test_cascade_races.py

Cada teste reproduz um incidente de 01/10/2026 com o interleaving que o
causou, forçado com asyncio.Event — sem rede, sem Supabase, sem RunPod:

  * Go 14:26 — llm-stack-705 criada enquanto a 704 era recriada;
  * Pro 13:12 — llm-stack-703 criada enquanto um startPod estava em voo;
  * Pro 10:00 — um único request recriou as duas Pro pausadas.

A regra que todos verificam: um pool põe de pé no máximo UMA máquina por vez.
"""

import asyncio
import os

import pytest

pytest.importorskip("fastapi", reason="importar main exige fastapi instalado")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import main  # noqa: E402


class NoGpu(Exception):
    def __str__(self):
        return "There are not enough free GPUs on the host machine to start this pod."


def _machine(mid, status, created_at):
    return {
        "id": mid, "name": mid, "status": status, "runpod_pod_id": f"pod-{mid}",
        "public_url": f"https://{mid}.invalid", "created_at": created_at,
        "last_activity_at": created_at,
        "templates": {"plan": "Pro", "category": "llm", "is_enabled": True, "is_test": False},
    }


class FakeSupa:
    """Banco em memória. list_wakeable devolve um snapshot único, como a
    consulta status=in.(creating,stopped) real."""

    def __init__(self, machines):
        self.machines = {m["id"]: m for m in machines}

    async def list_running_machines_for_plan(self, plan, category="llm"):
        return [m for m in self.machines.values() if m["status"] == "running"]

    async def list_wakeable_machines_for_plan(self, plan, category="llm"):
        return [dict(m) for m in sorted(self.machines.values(), key=lambda m: m["created_at"])
                if m["status"] in ("creating", "stopped")]

    # as duas consultas antigas, para o mesmo teste rodar contra o código de
    # antes da correção e mostrar a duplicata
    async def list_creating_machines_for_plan(self, plan, category="llm"):
        return [m for m in await self.list_wakeable_machines_for_plan(plan, category)
                if m["status"] == "creating"]

    async def list_stopped_machines_for_plan(self, plan, category="llm"):
        return [m for m in await self.list_wakeable_machines_for_plan(plan, category)
                if m["status"] == "stopped"]

    async def touch_machine_activity(self, machine_id):
        return None

    async def set_machine_status(self, machine_id, status, expected=None):
        self.machines[machine_id]["status"] = status
        return True

    async def log_machine_event(self, *a, **k):
        return None

    async def insert_provision_decision(self, row):
        return None


class FakeRunpod:
    """start_pod espera o Event da máquina (se houver) e então falha com
    no_gpu ou sobe, conforme configurado."""

    def __init__(self, no_gpu=(), gates=None):
        self.no_gpu = set(no_gpu)
        self.gates = gates or {}
        self.started = []

    async def start_pod(self, pod_id):
        gate = self.gates.get(pod_id)
        if gate is not None:
            await gate.wait()
        if pod_id in self.no_gpu:
            raise NoGpu()
        self.started.append(pod_id)


@pytest.fixture
def cascata(monkeypatch):
    """Zera o estado em memória do gateway e troca as bordas por fakes. Devolve
    um dict com as contagens de criação/recriação pedidas ao painel."""
    for name in ("last_wake_attempt", "last_wake_outcome", "recreating_in_progress",
                 "recreating_pool", "last_recreate_attempt", "provisioning_in_progress",
                 "last_provision_attempt"):
        monkeypatch.setattr(main, name, {})
    monkeypatch.setattr(main, "pending_recreates", set())
    monkeypatch.setattr(main, "PANEL_URL", "https://painel.invalid")
    monkeypatch.setattr(main, "PANEL_ADMIN_SECRET", "segredo")

    async def ligadas():
        return True

    monkeypatch.setattr(main, "machines_enabled", ligadas)
    monkeypatch.setattr(main, "schedule_key_sync", lambda machine_id: None)
    decisions = []
    monkeypatch.setattr(
        main, "_record_decision",
        lambda outcome, cause, trigger: decisions.append((outcome, cause)),
    )
    estado = {"recreated": [], "provisioned": [], "recreate_gate": asyncio.Event(),
              "decisions": decisions}

    async def recreate(machine_id, trigger=None):
        await estado["recreate_gate"].wait()
        estado["recreated"].append(machine_id)
        return {"machine_id": machine_id, "status": "creating"}

    async def provision(plan, category="llm", trigger=None):
        estado["provisioned"].append(plan)
        return {"machine_id": "nova", "name": "nova", "status": "creating"}

    monkeypatch.setattr(main, "recreate_machine_via_panel", recreate)
    monkeypatch.setattr(main, "provision_machine_for_plan", provision)
    return estado


async def _cascata_de_request(plan="Pro"):
    """O que resolve_base_machine faz com o pool sem máquina running: wake/
    recreate e, se nada subir, provisão. Devolve o desfecho do request."""
    woke = await main.wake_some_machine_for_plan(plan, "llm")
    if woke != "none":
        return woke
    if await main.try_provision_for_request(
        plan, "sem máquina para o modelo base", "llm",
        cause="provision.request.no_base_machine",
    ):
        return "provisioning"
    return "nada"


def test_request_durante_recriacao_nao_provisiona(cascata, monkeypatch):
    # Go 01/10 14:26: A dispara a recriação da 704 (host sem GPU); B chega com
    # o painel ainda recriando. Antes, B lia 'creating' antes do UPDATE e
    # 'stopped' depois, via o pool vazio e criava a 705.
    supa = FakeSupa([_machine("704", "stopped", "2026-10-01T13:27:18Z")])
    monkeypatch.setattr(main, "supa", supa, raising=False)
    monkeypatch.setattr(main, "runpod_client", FakeRunpod(no_gpu={"pod-704"}))

    async def cenario():
        a = await _cascata_de_request()
        # o painel vira a linha no meio da recriação, como o recreateMachine
        supa.machines["704"]["status"] = "creating"
        b = await _cascata_de_request()
        cascata["recreate_gate"].set()
        await asyncio.sleep(0.01)
        return a, b

    a, b = asyncio.run(cenario())

    assert a == "recreating"
    assert b in ("recreating", "waking")
    assert cascata["provisioned"] == []


def test_provisao_negada_com_recriacao_em_voo_no_pool(cascata, monkeypatch):
    # a guarda do gate, independente da leitura do banco: com uma recriação do
    # pool em voo, nenhuma provisão passa
    supa = FakeSupa([_machine("704", "stopped", "2026-10-01T13:27:18Z")])
    monkeypatch.setattr(main, "supa", supa, raising=False)
    monkeypatch.setattr(main, "runpod_client", FakeRunpod(no_gpu={"pod-704"}))

    async def cenario():
        await main.wake_some_machine_for_plan("Pro", "llm")
        ok = await main.try_provision_for_request(
            "Pro", "teste", "llm", cause="provision.request.no_base_machine",
        )
        cascata["recreate_gate"].set()
        await asyncio.sleep(0.01)
        return ok

    assert asyncio.run(cenario()) is False
    assert cascata["provisioned"] == []
    assert ("denied", "provision_denied.pool_recovering") in cascata["decisions"]


def test_wake_em_voo_conta_como_subindo(cascata, monkeypatch):
    # Pro 01/10 13:12: com o startPod de A ainda em voo, B lia o wake como
    # 'failed' e provisionava a 703 por cima
    gate = asyncio.Event()
    supa = FakeSupa([_machine("699", "stopped", "2026-09-30T18:27:00Z")])
    monkeypatch.setattr(main, "supa", supa, raising=False)
    monkeypatch.setattr(main, "runpod_client", FakeRunpod(gates={"pod-699": gate}))

    async def cenario():
        task_a = asyncio.create_task(_cascata_de_request())
        await asyncio.sleep(0.01)  # A parado dentro do start_pod
        b = await _cascata_de_request()
        gate.set()
        a = await task_a
        return a, b

    a, b = asyncio.run(cenario())

    assert a == "woke"
    assert b == "waking"
    assert cascata["provisioned"] == []


def test_wake_em_voo_que_termina_sem_gpu_recria_uma_so(cascata, monkeypatch):
    # variante do 13:12: o startPod em voo termina em no_gpu. Quem o disparou
    # recria; quem chegou no meio espera — nenhuma provisão, uma recriação
    gate = asyncio.Event()
    supa = FakeSupa([_machine("699", "stopped", "2026-09-30T18:27:00Z"),
                     _machine("pro", "stopped", "2026-09-30T23:39:00Z")])
    monkeypatch.setattr(main, "supa", supa, raising=False)
    monkeypatch.setattr(main, "runpod_client", FakeRunpod(
        no_gpu={"pod-699", "pod-pro"}, gates={"pod-699": gate}))

    async def cenario():
        tasks = [asyncio.create_task(_cascata_de_request()) for _ in range(3)]
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.sleep(0.01)
        cascata["recreate_gate"].set()
        return await asyncio.gather(*tasks)

    resultados = asyncio.run(cenario())

    assert cascata["provisioned"] == []
    assert cascata["recreated"] == ["699"]
    assert "provisioning" not in resultados


def test_um_request_recria_so_uma_das_pausadas(cascata, monkeypatch):
    # Pro 01/10 10:00: duas pausadas sem GPU no host; o laço seguia depois de
    # despachar a primeira recriação e recriava as duas para um request
    supa = FakeSupa([_machine("699", "stopped", "2026-09-30T18:27:00Z"),
                     _machine("pro", "stopped", "2026-09-30T23:39:00Z")])
    monkeypatch.setattr(main, "supa", supa, raising=False)
    monkeypatch.setattr(main, "runpod_client", FakeRunpod(no_gpu={"pod-699", "pod-pro"}))

    async def cenario():
        r = await _cascata_de_request()
        cascata["recreate_gate"].set()
        await asyncio.sleep(0.01)
        return r

    assert asyncio.run(cenario()) == "recreating"
    assert cascata["recreated"] == ["699"]
    assert main.pending_recreates == set()


def test_wake_cancelado_nao_fica_pending(cascata, monkeypatch):
    # cliente desconecta no meio do startPod: a marca não pode ficar
    # 'pending' segurando a cascata pelo cooldown inteiro
    gate = asyncio.Event()
    supa = FakeSupa([_machine("699", "stopped", "2026-09-30T18:27:00Z")])
    monkeypatch.setattr(main, "supa", supa, raising=False)
    monkeypatch.setattr(main, "runpod_client", FakeRunpod(gates={"pod-699": gate}))

    async def cenario():
        task = asyncio.create_task(main.wake_machine(
            supa.machines["699"], "teste", cause="wake.request.no_machine_available"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cenario())

    assert main.last_wake_outcome["699"] == "failed"
