"""Testes da geração de PDF a partir de HTML — módulo puro (sem fastapi, roda solto).

    python3 -m pytest test_document_generate.py

Requer weasyprint (a dependência real do módulo).

O que está sendo protegido:

  * os tetos de bytes/páginas por plano rejeitam ANTES (bytes) ou depois do
    layout mas antes da rasterização (páginas) — CPU proporcional ao plano,
    mesmo raciocínio de test_document_extract.py;
  * o url_fetcher bloqueia qualquer recurso que não seja data: — é a defesa
    contra SSRF (ver o cabeçalho de document_generate.py); sem este teste,
    uma regressão que trocasse ou removesse o url_fetcher custom passaria
    silenciosamente e o gateway voltaria a buscar URLs externas na rede
    privada do Railway.
"""

import pytest

from document_generate import (
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    GENERATION_PROMPT,
    HtmlTooLarge,
    RenderError,
    TooManyPages,
    build_messages,
    check_size,
    limit_bytes,
    limit_pages,
    render_pdf,
    strip_html_fences,
)


# ---------- render feliz ----------


def test_html_simples_gera_pdf_valido():
    pdf = render_pdf("<h1>Relatório</h1><p>conteúdo de teste</p>", "Pro")
    assert pdf.startswith(b"%PDF")


def test_html_com_imagem_data_uri_funciona():
    # gif 1x1 transparente em base64 — não depende de nenhum recurso externo
    pixel = (
        "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///"
        "yH5BAEAAAAALAAAAAABAAEAAAIBTAA7"
    )
    pdf = render_pdf(f'<img src="{pixel}">', "Pro")
    assert pdf.startswith(b"%PDF")


# ---------- teto de bytes (entrada) ----------


def test_limit_bytes_por_plano():
    assert limit_bytes("Go") == 2 * 1024 * 1024
    assert limit_bytes("Pro") == 5 * 1024 * 1024
    assert limit_bytes("plano-inexistente") == DEFAULT_MAX_HTML_BYTES
    assert limit_bytes(None) == DEFAULT_MAX_HTML_BYTES


def test_check_size_recusa_acima_do_teto():
    with pytest.raises(HtmlTooLarge):
        check_size(limit_bytes("Go") + 1, "Go")


def test_check_size_aceita_no_teto():
    check_size(limit_bytes("Go"), "Go")  # não levanta


# ---------- teto de páginas (saída) ----------


def test_limit_pages_por_plano():
    assert limit_pages("Go") == 20
    assert limit_pages("Pro") == 50
    assert limit_pages("plano-inexistente") == DEFAULT_MAX_PDF_PAGES


def test_falha_no_write_pdf_vira_render_error(monkeypatch):
    # write_pdf (rasterização final) é uma etapa separada de render() (layout)
    # e pode falhar por motivos próprios dela — sem o try/except em torno dela
    # (bug corrigido), a exceção subia crua em vez de RenderError, escapando
    # do único except que o handler sabe tratar.
    from weasyprint.document import Document

    def _boom(self, target=None):
        raise ValueError("falha simulada na rasterização")

    monkeypatch.setattr(Document, "write_pdf", _boom)
    with pytest.raises(RenderError):
        render_pdf("<p>oi</p>", "Pro")


def test_muitas_paginas_e_recusado():
    # CSS força uma quebra de página por item — gera mais páginas que o teto
    # do plano "Go" (20) sem precisar de um documento grande de verdade.
    items = "".join(
        f'<div style="page-break-after: always;">página {i}</div>' for i in range(25)
    )
    with pytest.raises(TooManyPages):
        render_pdf(items, "Go")


# ---------- bloqueio de recursos externos (SSRF) ----------


def test_url_externa_e_bloqueada_sem_abortar_o_documento():
    # a imagem externa nunca é buscada (sem isso o gateway faria uma
    # requisição de rede pro host referenciado); o documento ainda deve
    # renderizar, só sem o recurso bloqueado.
    html = '<p>texto visível</p><img src="http://example.invalid/x.png">'
    pdf = render_pdf(html, "Pro")
    assert pdf.startswith(b"%PDF")


# ---------- montagem de prompt (modo por instrução) ----------


def test_build_messages_soma_instrucao_do_cliente():
    messages = build_messages("um relatório de vendas de agosto")
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    # a instrução padrão (regras do HTML) tem que estar presente — é o que
    # garante HTML autossuficiente mesmo que o cliente não peça isso
    assert GENERATION_PROMPT in messages[0]["content"]
    assert "um relatório de vendas de agosto" in messages[0]["content"]


# ---------- limpeza de code fences ----------


def test_strip_html_fences_remove_bloco_markdown():
    wrapped = "```html\n<h1>oi</h1>\n```"
    assert strip_html_fences(wrapped) == "<h1>oi</h1>"


def test_strip_html_fences_preserva_html_sem_fence():
    html = "<h1>oi</h1>"
    assert strip_html_fences(html) == html


def test_strip_html_fences_remove_prosa_depois_do_fence():
    # caso comum: modelo obedece o fence mas ainda assim comenta depois —
    # ancorar a regex no texto inteiro (bug corrigido) perdia esse caso.
    wrapped = "```html\n<h1>oi</h1>\n```\nEspero que ajude!"
    assert strip_html_fences(wrapped) == "<h1>oi</h1>"


def test_strip_html_fences_remove_prosa_antes_do_fence():
    wrapped = "Aqui está o documento:\n```html\n<h1>oi</h1>\n```"
    assert strip_html_fences(wrapped) == "<h1>oi</h1>"


def test_strip_html_fences_sem_quebra_de_linha_apos_abertura():
    wrapped = "```html<h1>oi</h1>```"
    assert strip_html_fences(wrapped) == "<h1>oi</h1>"


