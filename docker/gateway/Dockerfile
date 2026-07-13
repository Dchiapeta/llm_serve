# Gateway estável de inferência — serviço separado dos pods (roda em CPU).
FROM python:3.12-slim

WORKDIR /opt/gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py routing.py supa.py .

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${GATEWAY_PORT:-8080}"]
