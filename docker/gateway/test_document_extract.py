"""Testes da extração de documento — módulo puro (sem fastapi, roda solto).

    python3 -m pytest test_document_extract.py

Requer pymupdf (a dependência real do módulo); pytesseract/tesseract NÃO são
necessários: os testes que passam por OCR verificam justamente que a falta do
binário degrada a página sem abortar o documento.

O que está sendo protegido:

  * os tetos por plano rejeitam ANTES do OCR — é o que impede uma requisição
    de consumir CPU desproporcional ao plano num processo que também atende
    todo o tráfego de chat;
  * OCR é decidido por PÁGINA, então PDF misto (capa escaneada + páginas
    digitais) não perde o texto embutido nem paga OCR à toa;
  * documento sem texto nenhum falha explicitamente em vez de virar prompt
    vazio — prompt vazio faria o modelo inventar um JSON que passa em
    qualquer validação sendo inteiramente falso.
"""

import fitz
import pytest

from document_extract import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    DEFAULT_MAX_DOCUMENT_PAGES,
    DocumentTooLarge,
    EmptyDocument,
    UnreadableDocument,
    build_messages,
    check_size,
    extract_text,
    limit_bytes,
    limit_pages,
)


def _pdf(*pages_text: str) -> bytes:
    """PDF em memória; string vazia = página sem texto embutido (o caso que
    dispara o caminho de OCR)."""
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=12)
    out = doc.tobytes()
    doc.close()
    return out


# ---------- texto embutido ----------


def test_pdf_digital_nao_usa_ocr():
    text, pages, ocr_used = extract_text(_pdf("NOTA FISCAL 12345"), "Pro")
    assert pages == 1
    assert ocr_used is False
    assert "12345" in text


def test_paginas_sao_concatenadas_na_ordem():
    text, pages, _ = extract_text(_pdf("primeira", "segunda", "terceira"), "Pro")
    assert pages == 3
    assert text.index("primeira") < text.index("segunda") < text.index("terceira")


# ---------- OCR por página ----------


def test_pagina_sem_texto_marca_ocr_sem_perder_o_resto():
    """PDF misto: a página vazia dispara OCR (que aqui devolve vazio, sem
    tesseract), mas o texto embutido das outras páginas continua valendo.
    Uma página ilegível não pode derrubar um documento de 20."""
    text, pages, ocr_used = extract_text(_pdf("pagina um", "", "pagina tres"), "Pro")
    assert pages == 3
    assert ocr_used is True
    assert "pagina um" in text and "pagina tres" in text


def test_documento_todo_sem_texto_falha_explicitamente():
    with pytest.raises(EmptyDocument):
        extract_text(_pdf("", ""), "Pro")


# ---------- documento inválido ----------


def test_arquivo_que_nao_e_pdf():
    with pytest.raises(UnreadableDocument):
        extract_text(b"isto nao e um pdf", "Pro")


def test_pdf_vazio_de_bytes():
    with pytest.raises(UnreadableDocument):
        extract_text(b"", "Pro")


# ---------- tetos por plano ----------


def test_teto_de_paginas_por_plano():
    pdf = _pdf(*[f"p{i}" for i in range(20)])
    # Pro (30) aceita; VibeCoder (15) recusa o MESMO documento
    _, pages, _ = extract_text(pdf, "Pro")
    assert pages == 20
    with pytest.raises(DocumentTooLarge):
        extract_text(pdf, "VibeCoder")


def test_plano_desconhecido_cai_no_default_conservador():
    """Plano novo nunca herda teto alto por esquecimento."""
    assert limit_pages("PlanoInexistente") == DEFAULT_MAX_DOCUMENT_PAGES
    assert limit_bytes(None) == DEFAULT_MAX_DOCUMENT_BYTES
    with pytest.raises(DocumentTooLarge):
        extract_text(_pdf(*[f"p{i}" for i in range(20)]), "PlanoInexistente")


def test_teto_de_paginas_rejeita_antes_do_ocr(monkeypatch):
    """A rejeição por páginas tem que vir ANTES de qualquer OCR: se viesse
    depois (ou por página), o custo de CPU já teria sido pago — e o teto
    deixaria de ser uma defesa.

    Provado espionando o _ocr_page: sem este espião o teste passaria mesmo com
    a checagem no lugar errado, porque em ambiente sem tesseract o OCR devolve
    string vazia em silêncio e o DocumentTooLarge apareceria de todo jeito."""
    import document_extract

    chamadas = []
    monkeypatch.setattr(
        document_extract, "_ocr_page", lambda page: chamadas.append(1) or ""
    )
    with pytest.raises(DocumentTooLarge):
        extract_text(_pdf(*["" for _ in range(20)]), "VibeCoder")
    assert not chamadas, "OCR rodou antes da checagem do teto de páginas"


def test_check_size():
    check_size(1024, "Pro")  # não levanta
    with pytest.raises(DocumentTooLarge):
        check_size(50 * 1024 * 1024, "Pro")


def test_check_size_usa_o_teto_do_plano():
    """8 MB passa no Pro (15 MB) e falha no VibeCoder (8 MB) — o mesmo upload,
    dois planos, tetos diferentes."""
    nove_mb = 9 * 1024 * 1024
    check_size(nove_mb, "Pro")
    with pytest.raises(DocumentTooLarge):
        check_size(nove_mb, "VibeCoder")


# ---------- prompt ----------


def test_build_messages_compoe_instrucao_do_cliente():
    """`user` do multipart entra ENTRE a instrução padrão e o documento —
    compõe, não substitui. Se substituísse, o cliente removeria sem querer o
    "não invente", que é a garantia contra campo fabricado."""
    content = build_messages("DOC", "Isto é um contrato de locação.")[0]["content"]
    assert "nunca invente" in content, "garantia do servidor foi perdida"
    assert "contrato de locação" in content
    assert content.index("nunca invente") < content.index("contrato de locação")
    assert content.index("contrato de locação") < content.index("DOC")


def test_build_messages_ignora_instrucao_em_branco():
    """String vazia/espaços não devem inserir um bloco de contexto vazio."""
    for vazio in (None, "", "   ", "\n"):
        assert "Contexto adicional" not in build_messages("DOC", vazio)[0]["content"]


def test_build_messages_embute_o_documento():
    msgs = build_messages("CONTEUDO EXTRAIDO")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "CONTEUDO EXTRAIDO" in msgs[0]["content"]


def test_prompt_manda_usar_null_em_vez_de_inventar():
    """Guided decoding garante a FORMA da saída, não a veracidade: sem esta
    instrução o modelo preenche o schema com valores plausíveis e o resultado
    passa em qualquer validação sendo falso."""
    assert "null" in build_messages("x")[0]["content"]
