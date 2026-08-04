"""Testes da política de conteúdo — módulo puro (sem fastapi, roda solto).

    python3 -m pytest test_content_policy.py

O que está sendo protegido: um bloco de conteúdo que o pod rejeita não pode
matar a SESSÃO. Cliente agêntico reenvia a conversa inteira a cada turno, então
um 400 num bloco antigo se repete indefinidamente — foi exatamente o que
aconteceu em produção no Pro (29/07/2026).
"""

from content_policy import MEDIA_DROPPED_NOTE, clamp_media, count_images, text_of


def _img(n: int = 1) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,AAA{n}"}}


def _user(*parts) -> dict:
    return {"role": "user", "content": list(parts)}


def _txt(t: str = "oi") -> dict:
    return {"type": "text", "text": t}


def _note_parts(messages: list) -> list[str]:
    """Textos que casam com o aviso de recorte, em todas as mensagens."""
    found = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            found += [
                p["text"] for p in content
                if isinstance(p, dict) and "removida" in str(p.get("text", ""))
            ]
        elif isinstance(content, str) and "removida" in content:
            found.append(content)
    return found


# ---------- caso normal: dentro do limite, nada muda ----------


def test_dentro_do_limite_passa_intacto():
    msgs = [_user(_txt(), _img(1)), _user(_txt(), _img(2))]
    out, dropped = clamp_media(msgs, 4)
    assert dropped == 0
    assert out == msgs
    assert not _note_parts(out)


def test_exatamente_no_limite_nao_recorta():
    msgs = [_user(_img(1), _img(2), _img(3), _img(4))]
    out, dropped = clamp_media(msgs, 4)
    assert dropped == 0 and count_images(out) == 4


def test_conversa_so_de_texto_nao_e_tocada():
    msgs = [{"role": "user", "content": "oi"}, {"role": "assistant", "content": "olá"}]
    out, dropped = clamp_media(msgs, 4)
    assert dropped == 0 and out == msgs


# ---------- recorte ----------


def test_recorta_o_excedente_e_conta_certo():
    msgs = [_user(*[_img(i) for i in range(6)])]
    out, dropped = clamp_media(msgs, 4)
    assert dropped == 2
    assert count_images(out) == 4


def test_mantem_as_imagens_MAIS_RECENTES():
    """Numa conversa longa, a imagem que o usuário acabou de mandar é a que ele
    quer que o modelo veja. Descartar a nova e manter as antigas seria o
    oposto do esperado."""
    msgs = [_user(_img(1)), _user(_img(2)), _user(_img(3))]
    out, dropped = clamp_media(msgs, 1)
    assert dropped == 2
    urls = [
        p["image_url"]["url"]
        for m in out
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if isinstance(p, dict) and p.get("type") == "image_url"
    ]
    assert urls == ["data:image/png;base64,AAA3"]


def test_aviso_explica_quantas_e_o_limite():
    msgs = [_user(_txt("o que tem nessas?"), *[_img(i) for i in range(6)])]
    out, _ = clamp_media(msgs, 4)
    notas = _note_parts(out)
    assert len(notas) == 1
    assert "2" in notas[0] and "4" in notas[0]


def test_texto_do_usuario_e_preservado_junto_do_aviso():
    msgs = [_user(_txt("olha isso"), _img(1), _img(2))]
    out, _ = clamp_media(msgs, 1)
    textos = [
        p["text"] for m in out for p in m["content"]
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    assert "olha isso" in textos


def test_aviso_entra_em_content_string_sem_quebrar_o_formato():
    """Última mensagem de user pode ter content string (nenhuma mídia nela);
    o aviso tem que virar texto concatenado, não uma lista."""
    msgs = [_user(_img(1), _img(2)), {"role": "user", "content": "e agora?"}]
    out, dropped = clamp_media(msgs, 1)
    assert dropped == 1
    assert isinstance(out[-1]["content"], str)
    assert "e agora?" in out[-1]["content"] and "removida" in out[-1]["content"]


# ---------- o bug de produção: a sessão não pode morrer ----------


def test_conversa_envenenada_produz_corpo_valido_sem_erro():
    """Regressão do bug do Pro: com imagem acima do teto, clamp_media NÃO pode
    levantar nada — tem que devolver um corpo que o pod aceita, pra que o turno
    seguinte funcione em vez de repetir 400 pra sempre."""
    msgs = [
        _user(_txt("como vc está?")),
        {"role": "assistant", "content": "tudo certo"},
        _user(_txt("o que tem nessa imagem?"), _img(1)),
        {"role": "assistant", "content": "..."},
        _user(_txt("oi")),
    ]
    out, dropped = clamp_media(msgs, 0)  # pod text-only
    assert dropped == 1
    assert count_images(out) == 0
    assert len(out) == len(msgs)  # nenhuma mensagem perdida
    assert _note_parts(out)  # o modelo tem como explicar


def test_limite_zero_remove_toda_imagem():
    msgs = [_user(_img(1)), _user(_img(2), _txt())]
    out, dropped = clamp_media(msgs, 0)
    assert dropped == 2 and count_images(out) == 0


# ---------- fail-open e entradas estranhas ----------


def test_limite_desconhecido_e_no_op():
    """Máquina anterior à migration (coluna NULL): sem saber o teto do pod, não
    recortar é o comportamento de hoje e o único seguro."""
    msgs = [_user(*[_img(i) for i in range(10)])]
    out, dropped = clamp_media(msgs, None)
    assert dropped == 0 and out == msgs


def test_entradas_degeneradas_nao_estouram():
    for entrada in (None, [], "nao sou lista", [None, 42, {"role": "user"}]):
        out, dropped = clamp_media(entrada, 2)
        assert dropped == 0


def test_nao_muta_a_entrada():
    original = [_user(_img(1), _img(2))]
    copia = [{"role": m["role"], "content": list(m["content"])} for m in original]
    clamp_media(original, 1)
    assert original == copia


# ---------- formato da Responses API (Codex) ----------


def test_responses_api_usa_input_image_e_input_text():
    """Errar o tipo da parte de texto faria o vLLM rejeitar justamente o aviso
    — o oposto do objetivo da função."""
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "olha"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
                {"type": "input_image", "image_url": "data:image/png;base64,BBB"},
            ],
        }
    ]
    out, dropped = clamp_media(msgs, 1, text_part_type="input_text")
    assert dropped == 1
    tipos = [p["type"] for p in out[0]["content"]]
    assert tipos.count("input_image") == 1
    assert "text" not in tipos  # nenhuma parte no formato errado
    assert tipos.count("input_text") == 2  # o original + o aviso


def test_note_template_tem_os_dois_placeholders():
    """Se alguém editar a mensagem e tirar um placeholder, o .format() explode
    em produção no meio de uma request."""
    assert "{n}" in MEDIA_DROPPED_NOTE and "{limit}" in MEDIA_DROPPED_NOTE


# ---------- text_of: o system prompt que sumia ----------


def test_text_of_string():
    assert text_of("você é um assistente jurídico") == "você é um assistente jurídico"


def test_text_of_lista_de_partes():
    """content em lista é protocolo OpenAI válido. Antes era descartado, e o
    system do cliente sumia junto com o fallback da stack."""
    content = [{"type": "text", "text": "parte um"}, {"type": "text", "text": " e dois"}]
    assert text_of(content) == "parte um e dois"


def test_text_of_ignora_partes_nao_texto():
    content = [_img(), {"type": "text", "text": "só isto"}]
    assert text_of(content) == "só isto"


def test_text_of_vazio_para_conteudo_sem_texto():
    """O caso que decide o fallback: sem texto aproveitável, quem chama trata
    como 'não veio system' e injeta o prompt da stack."""
    assert text_of("") == ""
    assert text_of([]) == ""
    assert text_of([_img()]) == ""
    assert text_of(None) == ""
    assert text_of({"type": "text", "text": "dict solto não é lista"}) == ""


def test_text_of_parte_malformada_nao_explode():
    """Parte sem "text" ou não-dict vem de cliente, não pode derrubar a request."""
    assert text_of([{"type": "text"}, "string solta", None, {"type": "text", "text": "ok"}]) == "ok"
