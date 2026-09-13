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


def test_picks_running_e_stopped_por_produto_excluem_teste_e_desabilitado():
    client = client_with_rest()

    asyncio.run(client.list_running_machines_for_plan("Go", "image"))
    asyncio.run(client.list_stopped_machines_for_plan("Go", "image"))

    assert len(client._rest.calls) == 2
    for path, params in client._rest.calls:
        assert path == "/machines"
        assert params["templates.plan"] == "eq.Go"
        assert params["templates.category"] == "eq.image"
        assert_production_filters(params)


def test_reposicao_proativa_percorre_plano_e_categoria_de_producao():
    client = client_with_rest([
        {"plan": "Go", "category": "llm"},
        {"plan": "Go", "category": "image"},
        {"plan": "Go", "category": "image"},
    ])

    products = asyncio.run(client.list_distinct_products())

    assert products == [("Go", "image"), ("Go", "llm")]
    path, params = client._rest.calls[0]
    assert path == "/templates"
    assert params["is_enabled"] == "eq.true"
    assert params["is_test"] == "eq.false"


def test_maquinas_subindo_por_produto_excluem_teste_e_desabilitado():
    # base do 'waking' de wake_some_machine_for_plan: uma máquina 'creating'
    # (recém-criada ou religada pelo auto-wake) segura a cascata pra não
    # religar/provisionar outra por cima do boot — mas só se for de produção
    client = client_with_rest([{"id": "m-boot"}])

    result = asyncio.run(client.list_creating_machines_for_plan("Pro", "llm"))

    assert result == [{"id": "m-boot"}]
    path, params = client._rest.calls[0]
    assert path == "/machines"
    assert params["status"] == "eq.creating"
    assert params["runpod_pod_id"] == "not.is.null"
    assert params["templates.plan"] == "eq.Pro"
    assert params["templates.category"] == "eq.llm"
    assert_production_filters(params)
