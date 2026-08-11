# Gateway estável de inferência — serviço separado dos pods (roda em CPU).
FROM python:3.12-slim

WORKDIR /opt/gateway

# tesseract: OCR das páginas escaneadas em /v1/documents/extract (o pytesseract
# é só o wrapper Python, o reconhecimento é o binário). O pacote -por é o
# idioma padrão da extração (ver OCR_LANGS em document_extract.py) — sem ele o
# tesseract falha ao ser chamado com lang=por, não faz fallback silencioso.
#
# libpango/libcairo/libgdk-pixbuf/libffi/shared-mime-info + fonts-dejavu-core:
# dependências de sistema do WeasyPrint (/v1/documents/generate, HTML → PDF).
# O pacote weasyprint no requirements.txt é só o binding Python — o layout e
# a rasterização são feitos por essas libs nativas, sem elas o import falha.
# fonts-dejavu-core garante ao menos uma fonte disponível pro fallback de
# texto sem CSS explícito (sem nenhuma fonte instalada, o PDF sai com texto
# ausente em vez de fonte genérica).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-por \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
        libffi-dev shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY docker/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Glob de propósito: a lista arquivo-a-arquivo que existia aqui já esqueceu
# módulo novo três vezes (anthropic_compat, recovery, e cli_policy +
# client_identity), e cada vez o gateway subiu em crash-loop com
# ModuleNotFoundError no import do main.py. Com o glob, módulo novo entra na
# imagem sozinho. O rm mantém o que a lista garantia de graça: teste fora da
# imagem. requirements.txt fica no COPY próprio acima para o pip continuar
# cacheado — este COPY invalida a cada mudança de código.
COPY docker/gateway/*.py .
RUN rm -f test_*.py

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${GATEWAY_PORT:-8080}"]
