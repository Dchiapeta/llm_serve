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

COPY docker/gateway/main.py docker/gateway/routing.py docker/gateway/supa.py docker/gateway/lifecycle.py docker/gateway/runpod_api.py docker/gateway/anthropic_compat.py docker/gateway/context_budget.py docker/gateway/usage_class.py docker/gateway/recovery.py docker/gateway/content_policy.py docker/gateway/document_extract.py docker/gateway/document_generate.py .

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${GATEWAY_PORT:-8080}"]
