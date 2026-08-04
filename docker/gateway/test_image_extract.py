"""Testes da extração de imagem — módulo puro (sem fastapi, roda solto).

    python3 -m pytest test_image_extract.py

Ao contrário de test_document_extract.py, estes testes de sucesso EXIGEM
pytesseract e o binário tesseract instalados: não há caminho "texto
embutido" pra imagem, toda chamada de extract_text_from_image passa por OCR
de verdade. Sem o binário, os testes que dependem de reconhecer texto real
falham (em vez de degradar em silêncio, como _ocr_page faz pro PDF).

O que está sendo protegido:

  * o teto de megapixels rejeita ANTES do OCR — defesa de CPU contra imagem
    de resolução absurda mesmo com poucos bytes em disco;
  * formato fora da lista suportada (ou arquivo que não é imagem) falha
    explicitamente como UnreadableDocument, não como exceção crua;
  * imagem sem texto reconhecível falha explicitamente em vez de virar
    prompt vazio.
"""

import pytest
from PIL import Image, ImageDraw

from document_extract import (
    DEFAULT_MAX_IMAGE_BYTES,
    MAX_IMAGE_MEGAPIXELS,
    DocumentTooLarge,
    EmptyDocument,
    UnreadableDocument,
    check_image_size,
    extract_text_from_image,
    limit_image_bytes,
)


def _image_bytes(text: str | None = None, size=(300, 100), fmt="PNG") -> bytes:
    import io

    img = Image.new("RGB", size, color="white")
    if text:
        draw = ImageDraw.Draw(img)
        draw.text((10, 30), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


# ---------- OCR ----------


def test_ocr_reconhece_texto_da_imagem():
    text, ocr_used = extract_text_from_image(_image_bytes("NOTA FISCAL 12345"), "Pro")
    assert ocr_used is True
    assert "12345" in text


def test_imagem_sem_texto_falha_explicitamente():
    with pytest.raises(EmptyDocument):
        extract_text_from_image(_image_bytes(), "Pro")


# ---------- formato/arquivo inválido ----------


def test_arquivo_que_nao_e_imagem():
    with pytest.raises(UnreadableDocument):
        extract_text_from_image(b"isto nao e uma imagem", "Pro")


def test_bytes_vazios():
    with pytest.raises(UnreadableDocument):
        extract_text_from_image(b"", "Pro")


def test_formato_nao_suportado():
    bmp = _image_bytes("texto", fmt="BMP")
    with pytest.raises(UnreadableDocument):
        extract_text_from_image(bmp, "Pro")


def test_webp_e_aceito():
    text, ocr_used = extract_text_from_image(
        _image_bytes("WEBP TEXTO", fmt="WEBP"), "Pro"
    )
    assert ocr_used is True


# ---------- teto de megapixels ----------


def test_teto_de_megapixels_rejeita_antes_do_ocr():
    """Assim como o teto de páginas do PDF, isto só é uma defesa de CPU de
    verdade se a checagem vier ANTES do pytesseract."""
    import pytesseract as _pt

    chamadas = []
    original = _pt.image_to_string
    _pt.image_to_string = lambda *a, **k: chamadas.append(1) or original(*a, **k)
    try:
        largura = 6000
        altura = MAX_IMAGE_MEGAPIXELS // largura + 100
        grande = _image_bytes(size=(largura, altura))
        with pytest.raises(DocumentTooLarge):
            extract_text_from_image(grande, "Pro")
        assert not chamadas, "OCR rodou antes da checagem de megapixels"
    finally:
        _pt.image_to_string = original


def test_teto_de_megapixels_rejeita_antes_do_decode():
    """width/height vêm do cabeçalho (Image.open não decodifica pixel
    nenhum) — o teto tem que rejeitar usando só isso, ANTES de chamar
    img.load(). Sem essa ordem, uma "decompression bomb" (arquivo pequeno em
    disco que descomprime pra uma imagem enorme) já teria pago o custo de
    CPU/memória do decode antes de ser rejeitada."""
    from PIL import Image as _Image

    largura = 6000
    altura = MAX_IMAGE_MEGAPIXELS // largura + 100
    # fixture gerada ANTES do monkeypatch: Image.new/save também chamam
    # .load() internamente, e isso não tem nada a ver com o que
    # extract_text_from_image faz — só nos interessa espionar a partir daqui.
    grande = _image_bytes(size=(largura, altura))

    chamadas = []
    original_load = _Image.Image.load
    _Image.Image.load = lambda self: chamadas.append(1) or original_load(self)
    try:
        with pytest.raises(DocumentTooLarge):
            extract_text_from_image(grande, "Pro")
        assert not chamadas, "a imagem foi decodificada antes da checagem de megapixels"
    finally:
        _Image.Image.load = original_load


# ---------- tetos de bytes por plano ----------


def test_check_image_size():
    check_image_size(1024, "Pro")  # não levanta
    with pytest.raises(DocumentTooLarge):
        check_image_size(50 * 1024 * 1024, "Pro")


def test_check_image_size_usa_o_teto_do_plano():
    """4 MB passa no Pro (10 MB) e falha no Go (5 MB) — o mesmo upload,
    dois planos, tetos diferentes."""
    quatro_mb = 4 * 1024 * 1024
    check_image_size(quatro_mb, "Pro")
    with pytest.raises(DocumentTooLarge):
        check_image_size(quatro_mb * 2, "Go")


def test_plano_desconhecido_cai_no_default_conservador():
    assert limit_image_bytes("PlanoInexistente") == DEFAULT_MAX_IMAGE_BYTES
    assert limit_image_bytes(None) == DEFAULT_MAX_IMAGE_BYTES
