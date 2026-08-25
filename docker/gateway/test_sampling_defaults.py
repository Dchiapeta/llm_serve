"""Testes de precedência e defaults de sampling (temperature/top_p/max_tokens/
presence_penalty) entre cliente, chave (api_keys, migration 0055) e stack
(stacks, migrations 0035/0056) — apply_key_sampling_defaults,
apply_stack_sampling_defaults, e a interação delas com o piso/teto de
max_tokens em validate_body/validate_responses_body. Rodar de
docker/gateway/:

    SUPABASE_URL=x SUPABASE_SERVICE_ROLE_KEY=y python3 -m pytest test_sampling_defaults.py

As env vars são só pra `import main` não estourar KeyError (main.py as lê
incondicionalmente no import, na definição de SUPABASE_URL/SERVICE_ROLE_KEY) —
nenhum caminho exercido aqui de fato usa Supabase ou rede: os testes que
chamam validate_body usam corpo sem "messages"/"prompt" (retorno antecipado,
antes de qualquer injeção de RAG/system prompt), e os que chamam
validate_responses_body stubam resolve_est_tokens/apply_context_budget e
mandam "instructions" já preenchido pra pular build_stack_instructions.
"""

import asyncio
import os

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

import main
from main import (
    MAX_MAX_TOKENS,
    MIN_MAX_TOKENS,
    apply_key_sampling_defaults,
    apply_stack_sampling_defaults,
    validate_body,
    validate_responses_body,
)


def _stack(id="s1", plan="go", **overrides):
    return {"id": id, "plan": plan, **overrides}


def _entry(stack=None, **key_overrides):
    return {
        "stack_id": stack["id"] if stack else None,
        "stacks": [stack] if stack else [],
        **key_overrides,
    }


def _machine(**overrides):
    return {"served_model_name": "test-model", "max_model_len": 32768, **overrides}


# ---------- 1. precedência: cliente explícito > chave > stack > global ----------


def test_precedencia_temperature_cliente_ganha_de_chave_e_stack():
    stack = _stack(default_temperature=0.2)
    entry = _entry(stack=stack, default_temperature=0.5)
    body = {"temperature": 0.9}
    apply_key_sampling_defaults(body, entry)
    apply_stack_sampling_defaults(body, entry)
    assert body["temperature"] == 0.9


def test_precedencia_temperature_chave_ganha_de_stack():
    stack = _stack(default_temperature=0.2)
    entry = _entry(stack=stack, default_temperature=0.5)
    body = {}
    apply_key_sampling_defaults(body, entry)
    apply_stack_sampling_defaults(body, entry)
    assert body["temperature"] == 0.5


def test_precedencia_temperature_stack_aplica_sem_default_de_chave():
    stack = _stack(default_temperature=0.2)
    entry = _entry(stack=stack)
    body = {}
    apply_key_sampling_defaults(body, entry)
    apply_stack_sampling_defaults(body, entry)
    assert body["temperature"] == 0.2


def test_precedencia_max_tokens_chave_ganha_de_stack_em_chat_completions():
    stack = _stack(default_max_tokens=500)
    entry = _entry(stack=stack, default_max_tokens=777)
    body = {}
    apply_key_sampling_defaults(body, entry)
    apply_stack_sampling_defaults(body, entry)
    assert body["max_tokens"] == 777


def test_precedencia_max_tokens_stack_usa_max_output_tokens_em_responses_api():
    stack = _stack(default_max_tokens=500)
    entry = _entry(stack=stack)
    body = {}
    apply_key_sampling_defaults(body, entry, max_tokens_field="max_output_tokens")
    apply_stack_sampling_defaults(body, entry, max_tokens_field="max_output_tokens")
    assert "max_tokens" not in body
    assert body["max_output_tokens"] == 500


# ---------- 4. presence_penalty da stack só em chat completions ----------


def test_presence_penalty_stack_aplicado_em_chat_completions():
    stack = _stack(default_presence_penalty=1.5)
    entry = _entry(stack=stack)
    body = {}
    apply_stack_sampling_defaults(body, entry)
    assert body["presence_penalty"] == 1.5


def test_presence_penalty_stack_nao_aplicado_em_responses_api():
    stack = _stack(default_presence_penalty=1.5)
    entry = _entry(stack=stack)
    body = {}
    apply_stack_sampling_defaults(body, entry, max_tokens_field="max_output_tokens")
    assert "presence_penalty" not in body


# ---------- 5. valores de borda ----------


def test_temperature_zero_explicito_nao_e_tratado_como_ausente():
    # 0.0 é um valor válido de temperature/top_p/presence_penalty — o guard
    # é sempre "not in body_json", nunca um check de truthy/falsy.
    stack = _stack(default_temperature=1.9)
    entry = _entry(stack=stack)
    body = {"temperature": 0.0}
    apply_stack_sampling_defaults(body, entry)
    assert body["temperature"] == 0.0


def test_presence_penalty_zero_explicito_nao_e_tratado_como_ausente():
    stack = _stack(default_presence_penalty=1.5)
    entry = _entry(stack=stack)
    body = {"presence_penalty": 0.0}
    apply_stack_sampling_defaults(body, entry)
    assert body["presence_penalty"] == 0.0


def test_default_max_tokens_zero_da_stack_e_aplicado_pelo_gateway():
    # default_max_tokens=0 nunca deveria chegar até aqui — é rejeitado nas
    # três camadas que guardam esse default (CHECK da migration 0056,
    # validateOptionalMaxTokens do TryStac, parseIntegerOrNull da rota
    # model-config/route.ts). Isso é diferente de um max_tokens=0 mandado
    # pelo CLIENTE direto no corpo de uma request de inferência — outro
    # contexto, que este teste não cobre. Documentando o comportamento caso
    # as três camadas acima sejam ignoradas/bypassadas: apply_stack_sampling_
    # defaults não re-valida, só checa "is not None", e aplicaria o 0.
    stack = _stack(default_max_tokens=0)
    entry = _entry(stack=stack)
    body = {}
    apply_stack_sampling_defaults(body, entry)
    assert body["max_tokens"] == 0


def test_stack_sem_override_nenhum_nao_mexe_no_body():
    stack = _stack()
    entry = _entry(stack=stack)
    body = {}
    apply_stack_sampling_defaults(body, entry)
    assert body == {}


# ---------- 6. chave sem stack resolvível ----------


def test_stack_id_sem_stack_correspondente_nao_levanta_excecao():
    entry = {"stack_id": "s1", "stacks": []}
    body = {"temperature": 0.5}
    apply_stack_sampling_defaults(body, entry)  # não deve levantar
    assert body == {"temperature": 0.5}


def test_sem_stack_id_nenhuma_stack_configurada():
    entry = {"stack_id": None, "stacks": []}
    body = {}
    apply_stack_sampling_defaults(body, entry)
    assert body == {}


# ---------- 2/3. assimetria do piso de max_tokens entre os dois endpoints ----------


def test_stack_max_tokens_abaixo_do_piso_e_respeitado_em_chat_completions():
    stack = _stack(default_max_tokens=MIN_MAX_TOKENS - 1000)
    entry = _entry(stack=stack)
    body = {}

    async def run():
        return await validate_body(
            body, entry, rewrite_model=False, machine=_machine(), stack_id="s1"
        )

    result = asyncio.run(run())
    assert result["max_tokens"] == MIN_MAX_TOKENS - 1000
    assert result["chat_template_kwargs"]["enable_thinking"] is False


def test_stack_max_tokens_acima_do_teto_e_clampado_em_chat_completions():
    stack = _stack(default_max_tokens=MAX_MAX_TOKENS + 5000)
    entry = _entry(stack=stack)
    body = {}

    async def run():
        return await validate_body(
            body, entry, rewrite_model=False, machine=_machine(), stack_id="s1"
        )

    result = asyncio.run(run())
    assert result["max_tokens"] == MAX_MAX_TOKENS


def test_stack_max_output_tokens_abaixo_do_piso_e_promovido_em_responses_api(monkeypatch):
    # Assimetria documentada em apply_stack_sampling_defaults e no plano: ao
    # contrário de chat completions, aqui não existe o branch de "respeitar
    # e desligar thinking" — qualquer valor abaixo do piso é promovido.
    async def _stub_resolve_est_tokens(*args, **kwargs):
        return 0, None

    monkeypatch.setattr(main, "resolve_est_tokens", _stub_resolve_est_tokens)
    monkeypatch.setattr(main, "apply_context_budget", lambda *a, **k: None)

    stack = _stack(default_max_tokens=MIN_MAX_TOKENS - 1000)
    entry = _entry(stack=stack)
    body = {"instructions": "já preenchido — pula build_stack_instructions (rede)"}

    async def run():
        return await validate_responses_body(
            body, entry, rewrite_model=False, machine=_machine(), stack_id="s1"
        )

    result = asyncio.run(run())
    assert result["max_output_tokens"] == MIN_MAX_TOKENS


def test_stack_max_output_tokens_acima_do_teto_e_clampado_em_responses_api(monkeypatch):
    async def _stub_resolve_est_tokens(*args, **kwargs):
        return 0, None

    monkeypatch.setattr(main, "resolve_est_tokens", _stub_resolve_est_tokens)
    monkeypatch.setattr(main, "apply_context_budget", lambda *a, **k: None)

    stack = _stack(default_max_tokens=MAX_MAX_TOKENS + 5000)
    entry = _entry(stack=stack)
    body = {"instructions": "já preenchido — pula build_stack_instructions (rede)"}

    async def run():
        return await validate_responses_body(
            body, entry, rewrite_model=False, machine=_machine(), stack_id="s1"
        )

    result = asyncio.run(run())
    assert result["max_output_tokens"] == MAX_MAX_TOKENS
