"""Contrato dos filtros de disponibilidade usados pelo roteamento automático."""

import asyncio

from supa import SupaClient


class FakeResponse:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self.rows


class FakeRest:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    async def get(self, path, params):
        self.calls.append((path, params))
        return FakeResponse(self.rows)


def client_with_rest(rows=None):
    client = object.__new__(SupaClient)
    client._rest = FakeRest(rows)
    return client


def assert_production_filters(params):
    assert params["templates.is_enabled"] == "eq.true"
    assert params["templates.is_test"] == "eq.false"


def test_pick_global_so_enxerga_maquinas_de_producao():
    client = client_with_rest([{"id": "m-prod"}])

    result = asyncio.run(client.list_routable_running_machines())

    assert result == [{"id": "m-prod"}]
    path, params = client._rest.calls[0]
    assert path == "/machines"
    assert_production_filters(params)
    assert "templates!inner" in params["select"]


def test_get_machine_traz_flags_para_bloquear_recriacao_automatica():
    client = client_with_rest([{"id": "m1", "templates": {"is_test": True}}])

    result = asyncio.run(client.get_machine("m1"))

    assert result["id"] == "m1"
    _path, params = client._rest.calls[0]
    assert "templates(is_enabled,is_test)" in params["select"]


def test_picks_running_e_stopped_por_plano_excluem_teste_e_desabilitado():
    client = client_with_rest()

    asyncio.run(client.list_running_machines_for_plan("Pro"))
    asyncio.run(client.list_stopped_machines_for_plan("Pro"))

    assert len(client._rest.calls) == 2
    for path, params in client._rest.calls:
        assert path == "/machines"
        assert params["templates.plan"] == "eq.Pro"
        assert_production_filters(params)


def test_reposicao_proativa_so_percorre_planos_de_producao():
    client = client_with_rest([{"plan": "Pro"}, {"plan": "Pro"}, {"plan": "Go"}])

    plans = asyncio.run(client.list_distinct_plans())

    assert plans == ["Go", "Pro"]
    path, params = client._rest.calls[0]
    assert path == "/templates"
    assert params["is_enabled"] == "eq.true"
    assert params["is_test"] == "eq.false"
