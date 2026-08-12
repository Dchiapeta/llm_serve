"""Testes da resolução de system prompt por chave (key_prompt.py) — módulo puro.

    python3 -m pytest test_key_prompt.py
"""

from key_prompt import resolve_system_prompt

STACK = {"id": "s1", "system_prompt": "Você é o assistente da Acme."}
KEY_PROMPT = "Você resume e-mails em uma frase."


def _entry(**overrides):
    entry = {
        "api_key_id": "k1",
        "stack_id": "s1",
        "use_custom_prompt": False,
        "system_prompt": None,
    }
    entry.update(overrides)
    return entry


def test_switch_desligado_usa_o_prompt_da_stack():
    """O caso de toda chave existente: a migration 0053 não muda nada pra elas."""
    assert resolve_system_prompt(_entry(), STACK) == STACK["system_prompt"]


def test_switch_desligado_ignora_o_texto_guardado():
    """O rascunho sobrevive ao desligar o switch, mas não vale pra request."""
    entry = _entry(use_custom_prompt=False, system_prompt=KEY_PROMPT)
    assert resolve_system_prompt(entry, STACK) == STACK["system_prompt"]


def test_switch_ligado_usa_o_prompt_da_chave():
    entry = _entry(use_custom_prompt=True, system_prompt=KEY_PROMPT)
    assert resolve_system_prompt(entry, STACK) == KEY_PROMPT


def test_switch_ligado_sem_texto_cai_na_stack():
    """Ligado e vazio é estado alcançável pela UI (ligou, não escreveu). Vale o
    da stack — instrução vazia não apaga a configuração da conta."""
    for vazio in (None, "", "   \n  "):
        entry = _entry(use_custom_prompt=True, system_prompt=vazio)
        assert resolve_system_prompt(entry, STACK) == STACK["system_prompt"]


def test_prompt_da_chave_vale_mesmo_sem_prompt_na_stack():
    stack = {"id": "s1", "system_prompt": None}
    entry = _entry(use_custom_prompt=True, system_prompt=KEY_PROMPT)
    assert resolve_system_prompt(entry, stack) == KEY_PROMPT


def test_sem_prompt_em_lugar_nenhum_devolve_none():
    """None e não "": quem chama testa a verdade do valor pra decidir se injeta
    a mensagem de system — string vazia injetaria um system inútil."""
    assert resolve_system_prompt(_entry(), {"id": "s1", "system_prompt": ""}) is None
    assert resolve_system_prompt(_entry(), None) is None


def test_texto_e_devolvido_sem_espaco_em_branco_nas_pontas():
    entry = _entry(use_custom_prompt=True, system_prompt=f"\n  {KEY_PROMPT}  \n")
    assert resolve_system_prompt(entry, STACK) == KEY_PROMPT


def test_chave_antiga_sem_as_colunas_novas_cai_na_stack():
    """Entry do key_cache preenchido antes do deploy (ou por um find_active_key
    mais velho) não tem as chaves — não pode virar KeyError na hot path."""
    entry = {"api_key_id": "k1", "stack_id": "s1"}
    assert resolve_system_prompt(entry, STACK) == STACK["system_prompt"]
