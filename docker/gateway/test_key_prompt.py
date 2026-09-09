"""Testes da resolução do prompt configurado por chave (key_prompt.py) — módulo puro.

    python3 -m pytest test_key_prompt.py
"""

import base64
import pathlib
import re

from key_prompt import (
    IMAGE_PROMPT_HEADER,
    MAX_PROMPT_HEADER_BYTES,
    encode_prompt_header,
    resolve_image_prompt,
    resolve_system_prompt,
)

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


# ---------------------------------------------------------------------------
# imagem: o `prompt` do corpo no papel do `system`
# ---------------------------------------------------------------------------


def test_imagem_prompt_do_cliente_ganha():
    entry = _entry(use_custom_prompt=True, system_prompt=KEY_PROMPT)
    assert resolve_image_prompt("um gato", entry, STACK) == "um gato"


def test_imagem_sem_prompt_no_corpo_usa_o_da_chave():
    entry = _entry(use_custom_prompt=True, system_prompt=KEY_PROMPT)
    assert resolve_image_prompt(None, entry, STACK) == KEY_PROMPT


def test_imagem_sem_prompt_no_corpo_e_sem_o_da_chave_usa_o_da_stack():
    assert resolve_image_prompt(None, _entry(), STACK) == STACK["system_prompt"]


def test_imagem_prompt_em_branco_nao_apaga_o_configurado():
    """Mesma regra do client_system_text do chat: campo vazio não é instrução,
    e não pode zerar em silêncio o que a conta configurou."""
    entry = _entry(use_custom_prompt=True, system_prompt=KEY_PROMPT)
    for vazio in (None, "", "   \n ", 42, [], {}):
        assert resolve_image_prompt(vazio, entry, STACK) == KEY_PROMPT


def test_imagem_prompt_do_cliente_nao_e_reescrito():
    """Sem strip: o campo é do cliente, e o gateway só decide se ele conta."""
    assert resolve_image_prompt("  um gato  ", _entry(), STACK) == "  um gato  "


def test_imagem_sem_prompt_em_lugar_nenhum_devolve_none():
    """None e não erro: quem responde 400 (`missing_prompt`) é o pod, que é
    quem valida o resto do corpo."""
    assert resolve_image_prompt(None, _entry(), {"id": "s1", "system_prompt": ""}) is None


# ---------------------------------------------------------------------------
# header do /v1/images/edits
# ---------------------------------------------------------------------------


def _decode(value: str) -> str:
    return base64.b64decode(value).decode("utf-8")


def test_header_carrega_o_texto_de_ida_e_volta():
    assert _decode(encode_prompt_header(KEY_PROMPT)) == KEY_PROMPT


def test_header_sobrevive_a_acento_emoji_e_quebra_de_linha():
    """O motivo do base64: nada disso cabe cru num header HTTP."""
    texto = "prova a camisa 🧥\nsem mudar o fundo — cor: açaí"
    assert _decode(encode_prompt_header(texto)) == texto


def test_sem_prompt_nao_ha_header():
    """None e não "": o call site testa a verdade do valor para decidir se
    manda o header, e um header vazio faria o pod distinguir "" de ausente."""
    assert encode_prompt_header(None) is None
    assert encode_prompt_header("") is None


def test_header_e_truncado_no_teto():
    gigante = "a" * (MAX_PROMPT_HEADER_BYTES * 3)
    assert len(_decode(encode_prompt_header(gigante))) == MAX_PROMPT_HEADER_BYTES


def test_truncagem_nao_produz_utf8_invalido():
    """O corte é em BYTES e cai no meio de um caractere multibyte — sem o
    descarte do pedaço órfão o pod jogaria fora o prompt INTEIRO."""
    # cada 🧥 são 4 bytes: o teto cai no meio de um deles
    gigante = "🧥" * MAX_PROMPT_HEADER_BYTES
    texto = _decode(encode_prompt_header(gigante))  # levanta se for inválido
    assert texto.startswith("🧥")
    assert len(texto.encode("utf-8")) <= MAX_PROMPT_HEADER_BYTES


def test_o_pod_le_o_header_com_o_mesmo_nome():
    """Contrato entre imagens Docker diferentes, sem código em comum.

    O gateway ESCREVE IMAGE_PROMPT_HEADER e o pod LÊ policy.PROMPT_HEADER. Um
    import cruzado não existe (são duas imagens), então nada além deste teste
    denuncia uma renomeação de um lado só — e o sintoma seria o prompt
    configurado parar de chegar ao `edits`, em silêncio, com o 400 apontando
    para o cliente.

    Lê o arquivo em vez de importar: `policy` do pod tem o mesmo tipo de nome
    genérico que outros módulos do repo, e mexer no sys.path daqui para
    importá-lo trocaria um risco por outro.
    """
    policy_py = (
        pathlib.Path(__file__).resolve().parent.parent / "image" / "policy.py"
    ).read_text(encoding="utf-8")
    achado = re.search(r'^PROMPT_HEADER = "([^"]+)"', policy_py, re.MULTILINE)
    assert achado, "docker/image/policy.py não define mais PROMPT_HEADER"
    # case-insensitive: header HTTP não distingue caixa, e os dois lados a
    # escrevem de formas diferentes de propósito (constante vs. lookup do
    # Starlette, que normaliza para minúsculas).
    assert achado.group(1).lower() == IMAGE_PROMPT_HEADER.lower()
