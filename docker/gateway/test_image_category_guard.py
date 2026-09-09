"""Guard de categoria em authenticate: o que uma stack de imagem pode chamar.

    python3 -m pytest test_image_category_guard.py

Este guard não mora nas rotas de /v1/images/* — mora no authenticate, que é o
tronco de TODAS as rotas autenticadas. Por isso não cabe em test_image_routes.py
(que substitui authenticate por um duplo de propósito): aqui o authenticate roda
de verdade, com o key_cache pré-populado no lugar do Supabase.

O que ele decide é assimétrico e vale testar nos dois sentidos:

  * uma stack de categoria image NÃO pode chamar endpoint de LLM — o pod dela é
    de difusão e não fala /v1/chat/completions;
  * mas PODE chamar /v1/models, que o pod de difusão implementa (server.py) e o
    agent tem na allowlist. Barrar seria o gateway fechando uma porta que as
    três camadas abaixo abrem, e todo cliente OpenAI-compatível chama /v1/models
    antes de gerar.
"""

import asyncio
import hashlib
import os
import time

import pytest

pytest.importorskip("fastapi", reason="importar main exige fastapi instalado")
pytest.importorskip("jsonschema", reason="importar main exige jsonschema")

# Mesmo motivo de test_image_routes.py: main.py lê as duas no IMPORT.
os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-de-teste")

import main  # noqa: E402

CHAVE = "sk-teste-categoria"
STACK_ID = "stack-de-imagem"


def _cachear(stack: dict) -> None:
    """Põe a chave no key_cache já resolvida — authenticate nunca chega ao supa.

    O TTL futuro é o que faz o ramo de cache vencer; sem ele authenticate
    tentaria find_active_key e o teste viraria um teste de rede."""
    entry = {
        "account_id": "acc-1",
        "api_key_id": "key-1",
        "stack_id": STACK_ID,
        "stacks": [stack],
        "purpose": "customer",
    }
    key_hash = hashlib.sha256(CHAVE.encode()).hexdigest()
    # Relógio real e não um mock: o TTL é comparado contra time.time() dentro do
    # authenticate, e 300s cobre qualquer duração plausível da suíte.
    main.key_cache[key_hash] = (entry, time.time() + 300)


def _stack(category: str | None, plan: str = "Go") -> dict:
    s = {"id": STACK_ID, "plan": plan}
    if category is not None:
        s["category"] = category
    return s


@pytest.fixture(autouse=True)
def _isola(monkeypatch):
    main.key_cache.clear()
    # enforce_client_limit é async e consulta estado de ambientes; o guard de
    # categoria decide antes dele, e nos casos que PASSAM ele só adicionaria
    # I/O sem relação com o que está sendo testado.
    async def _sem_teto(entry, stack, plan, headers):
        return None

    monkeypatch.setattr(main, "enforce_client_limit", _sem_teto)
    yield
    main.key_cache.clear()


def _auth(path: str):
    return asyncio.run(main.authenticate(f"Bearer {CHAVE}", {}, path))


@pytest.mark.parametrize(
    "path", ["chat/completions", "completions", "embeddings", "responses"]
)
def test_stack_de_imagem_e_barrada_em_endpoint_de_llm(path):
    """Sem o guard a requisição resolveria a máquina DELA — um pod de difusão —
    e morreria num 404 três camadas abaixo de onde dá pra explicar."""
    _cachear(_stack("image"))
    with pytest.raises(main.HTTPException) as e:
        _auth(path)
    assert e.value.status_code == 403
    assert "geração de imagem" in e.value.detail


@pytest.mark.parametrize("path", ["images/generations", "images/edits"])
def test_stack_de_imagem_passa_nas_rotas_de_imagem(path):
    _cachear(_stack("image"))
    entry, _key_hash = _auth(path)
    assert entry["stack_id"] == STACK_ID


def test_stack_de_imagem_passa_em_models():
    """A regressão que este arquivo existe para travar: /v1/models é servido
    pelo pod de difusão (docker/image/server.py) e está na allowlist do agent.
    Um 403 aqui quebraria a descoberta de modelo de qualquer cliente
    OpenAI-compatível antes da primeira geração."""
    _cachear(_stack("image"))
    entry, _key_hash = _auth("models")
    assert entry["stack_id"] == STACK_ID


@pytest.mark.parametrize("path", ["chat/completions", "models", "images/generations"])
def test_stack_de_llm_nao_e_afetada_pelo_guard(path):
    """O guard é unidirecional: quem barra uma chave de LLM em /v1/images/* é o
    _require_image_product, lá na rota, com mensagem própria — não este."""
    _cachear(_stack("llm", plan="Pro"))
    entry, _key_hash = _auth(path)
    assert entry["stack_id"] == STACK_ID


def test_categoria_ausente_e_tratada_como_llm():
    """Linha sem o campo (mock antigo, ou leitura anterior à 0060) não pode
    virar stack de imagem por omissão — o default do banco é 'llm'."""
    _cachear(_stack(None))
    entry, _key_hash = _auth("chat/completions")
    assert entry["stack_id"] == STACK_ID
