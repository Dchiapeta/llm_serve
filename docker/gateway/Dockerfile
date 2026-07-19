# Gateway estável de inferência — serviço separado dos pods (roda em CPU).
FROM python:3.12-slim

WORKDIR /opt/gateway

COPY docker/gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/gateway/main.py docker/gateway/routing.py docker/gateway/supa.py docker/gateway/lifecycle.py docker/gateway/runpod_api.py docker/gateway/anthropic_compat.py docker/gateway/context_budget.py docker/gateway/usage_class.py .

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${GATEWAY_PORT:-8080}"]
