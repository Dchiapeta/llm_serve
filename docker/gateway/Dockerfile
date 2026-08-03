# Gateway estável de inferência — serviço separado dos pods (roda em CPU).
FROM python:3.12-slim

WORKDIR /opt/gateway

# tesseract: OCR das páginas escaneadas em /v1/documents/extract (o pytesseract
# é só o wrapper Python, o reconhecimento é o binário). O pacote -por é o
# idioma padrão da extração (ver OCR_LANGS em document_extract.py) — sem ele o
# tesseract falha ao ser chamado com lang=por, não faz fallback silencioso.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

COPY docker/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/gateway/main.py docker/gateway/routing.py docker/gateway/supa.py docker/gateway/lifecycle.py docker/gateway/runpod_api.py docker/gateway/anthropic_compat.py docker/gateway/context_budget.py docker/gateway/usage_class.py docker/gateway/recovery.py docker/gateway/content_policy.py docker/gateway/document_extract.py .

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${GATEWAY_PORT:-8080}"]
