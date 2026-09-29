"""Rastreamento de causa do ciclo de vida (trigger_ctx.py + decisions.py) —
sem fastapi, sem rede. Rodar de docker/gateway/:

    python3 -m pytest test_trigger_trace.py

O que estes testes protegem é a resposta que faltou na llm-stack-505 (16/09):
"qual chave fez esta máquina nascer" tem que sobreviver do authenticate até a
task de background que grava o evento, e as quatro negações de
_try_provision_machine_for_plan têm que deixar rastro. A fiação em main.py
não é testável aqui (import main exige fastapi, ausente localmente) — é
coberta pela reprodução manual descrita no plano.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import decisions
import recovery
import trigger_ctx
from generation_trace import request_trace_id
from trigger_ctx import set_request_trigger, snapshot

ENTRY = {
    "account_id": "acc-1",
    "account_name": "Cliente Teste",
    "api_key_id": "key-1",
    "key_prefix": "stac_150",
    "key_hash": "NUNCA-SAI-DAQUI",
    "stack_id": "stack-1",
    "stacks": [{"id": "stack-1", "slug": "crisp-reef-83", "plan": "Go", "category": "llm"}],
    "purpose": "customer",
}


class FakeSupa:
    def __init__(self):
        self.rows: list[dict] = []
        self.deleted_before: list[str] = []

    async def insert_provision_decision(self, row):
        self.rows.append(row)

    async def delete_provision_decisions_before(self, cutoff_iso):
        self.deleted_before.append(cutoff_iso)


@pytest.fixture(autouse=True)
def _estado_limpo():
    decisions._last.clear()
    decisions._locks.clear()
    yield
    decisions._last.clear()
    decisions._locks.clear()


def run(coro):
    return asyncio.run(coro)


async def _drain():
    # deixa as tasks fire-and-forget (spawn_tracked) terminarem
    pending = [t for t in recovery._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending)


# ---------- envelope ----------


def test_snapshot_carrega_chave_originadora():
    async def go():
        set_request_trigger(
            ENTRY, plan="Go", category="llm", path="chat/completions", user_agent="n8n/1.0",
        )
        return snapshot("provision.request.no_base_machine", reason="sem máquina para o modelo base")

    trig = run(go())
    assert trig["actor"] == "request"
    assert trig["cause"] == "provision.request.no_base_machine"
    assert trig["api_key_id"] == "key-1"
    assert trig["key_prefix"] == "stac_150"
    assert trig["stack_id"] == "stack-1"
    assert trig["stack_slug"] == "crisp-reef-83"
    assert trig["account_id"] == "acc-1"
    assert trig["plan"] == "Go"
    assert trig["user_agent"] == "n8n/1.0"
    assert trig["reason"] == "sem máquina para o modelo base"
    # nunca a chave em claro nem o hash
    assert "key_hash" not in trig
    assert "NUNCA-SAI-DAQUI" not in repr(trig)


def test_snapshot_sobrevive_a_task_filha():
    """create_task copia o contexto no instante da criação: o filho ainda vê
    o originador depois de o pai resetar — é o que faz _provision_and_track
    saber quem pediu a máquina mesmo com o 503 já respondido."""
    async def go():
        set_request_trigger(ENTRY, plan="Go", category="llm", path="p", user_agent=None)
        seen = {}

        async def child():
            await asyncio.sleep(0)
            seen.update(snapshot("provision.request.no_base_machine"))

        task = recovery.spawn_tracked(child())
        trigger_ctx.trigger_ctx.set(None)  # o request "acabou"
        await task
        return seen

    seen = run(go())
    assert seen["api_key_id"] == "key-1"
    assert seen["actor"] == "request"


def test_snapshot_nao_compartilha_dict():
    async def go():
        set_request_trigger(ENTRY, plan="Go", category="llm", path="p", user_agent=None)
        s = snapshot("x")
        s["api_key_id"] = "ALTERADO"
        s["novo"] = 1
        return trigger_ctx.trigger_ctx.get()

    ctx = run(go())
    assert ctx["api_key_id"] == "key-1"
    assert "novo" not in ctx


def test_contexto_vazio_e_lifecycle():
    trig = snapshot("provision.pool.refill", plan="Pro", category="llm")
    assert trig["actor"] == "lifecycle"
    assert "api_key_id" not in trig
    assert trig["plan"] == "Pro"


def test_envelope_rejeita_chave_fora_da_allowlist():
    trig = snapshot("x", authorization="Bearer stac_segredo", body={"messages": []})
    assert "authorization" not in trig
    assert "body" not in trig
    assert "segredo" not in repr(trig)


def test_snapshot_leva_trace_id_da_request():
    token = request_trace_id.set("trace-abc")
    try:
        assert snapshot("x")["trace_id"] == "trace-abc"
    finally:
        request_trace_id.reset(token)


def test_user_agent_truncado():
    async def go():
        set_request_trigger(ENTRY, plan="Go", category="llm", path="p", user_agent="u" * 500)
        return snapshot("x")["user_agent"]

    assert len(run(go())) == trigger_ctx.MAX_USER_AGENT_CHARS


# ---------- gate ----------


def test_gate_switch_off_lock_cooldown_panel():
    base = dict(switch_on=True, panel_configured=True, lock_active=False,
                last_attempt=0.0, now=1000.0, cooldown_s=60.0)
    assert decisions.provision_gate(**base) is None
    assert decisions.provision_gate(**{**base, "switch_on": False}) == "provision_denied.switch_off"
    # caminho de request ignora SÓ o interruptor
    assert decisions.provision_gate(**{**base, "switch_on": False}, ignore_switch=True) is None
    assert decisions.provision_gate(**{**base, "panel_configured": False}) == "provision_denied.panel_unconfigured"
    assert decisions.provision_gate(**{**base, "lock_active": True}) == "provision_denied.lock_active"
    assert decisions.provision_gate(**{**base, "last_attempt": 990.0}) == "provision_denied.cooldown"
    # ordem: painel antes da trava, trava antes do cooldown
    assert decisions.provision_gate(
        **{**base, "panel_configured": False, "lock_active": True, "last_attempt": 999.0}
    ) == "provision_denied.panel_unconfigured"
    assert decisions.provision_gate(
        **{**base, "lock_active": True, "last_attempt": 999.0}
    ) == "provision_denied.lock_active"


# ---------- gravação ----------


def test_record_bg_grava_linha_com_originador():
    supa = FakeSupa()

    async def go():
        set_request_trigger(ENTRY, plan="Go", category="llm", path="p", user_agent="ua")
        trig = snapshot("provision.request.no_base_machine")
        assert decisions.record_bg(supa, decisions.OUTCOME_GRANTED, trig["cause"], trig)
        await _drain()

    run(go())
    assert len(supa.rows) == 1
    row = supa.rows[0]
    assert row["outcome"] == "granted"
    assert row["cause"] == "provision.request.no_base_machine"
    assert row["actor"] == "request"
    assert row["api_key_id"] == "key-1"
    assert row["stack_id"] == "stack-1"
    assert row["plan"] == "Go"
    assert row["trigger_meta"]["key_prefix"] == "stac_150"
    assert row["repeat_count"] == 1


def test_record_bg_dedupe():
    supa = FakeSupa()
    trig = {"actor": "request", "stack_id": "stack-1", "plan": "Go", "category": "llm"}

    # 1ª grava; 2ª e 3ª na janela são suprimidas
    assert decisions.should_record("denied_503.provisioning", trig, now=100.0) == 1
    assert decisions.should_record("denied_503.provisioning", trig, now=110.0) is None
    assert decisions.should_record("denied_503.provisioning", trig, now=120.0) is None
    # fora da janela: grava de novo levando as 2 suprimidas
    assert decisions.should_record("denied_503.provisioning", trig, now=200.0) == 3
    # causa diferente ou stack diferente não colidem
    assert decisions.should_record("denied_503.waking", trig, now=200.0) == 1
    assert decisions.should_record("denied_503.provisioning", {**trig, "stack_id": "s2"}, now=200.0) == 1

    async def go():
        decisions.record_bg(supa, "served_503", "denied_503.capacity", trig)
        decisions.record_bg(supa, "served_503", "denied_503.capacity", trig)
        await _drain()

    run(go())
    assert len(supa.rows) == 1


def test_record_bg_sem_loop_nao_explode():
    # fora de um event loop (chamada síncrona em teste) devolve False e segue
    assert decisions.record_bg(FakeSupa(), "denied", "x", {"stack_id": "s"}) is False


def test_falha_de_gravacao_nao_propaga():
    class Quebrado(FakeSupa):
        async def insert_provision_decision(self, row):
            raise RuntimeError("supabase fora")

    async def go():
        decisions.record_bg(Quebrado(), "denied", "y", {"stack_id": "s"})
        await _drain()

    run(go())  # sem exceção


# ---------- retenção ----------


def test_prune_decisions_usa_cutoff():
    supa = FakeSupa()
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    assert run(decisions.prune_decisions_once(supa, now=now)) is True
    assert supa.deleted_before == [(now - timedelta(days=decisions.RETENTION_DAYS)).isoformat()]


def test_prune_respeita_trava():
    supa = FakeSupa()
    decisions._locks[decisions._LOCK_KEY] = __import__("time").time()
    assert run(decisions.prune_decisions_once(supa)) is False
    assert supa.deleted_before == []
