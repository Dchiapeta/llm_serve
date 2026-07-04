#!/bin/bash
set -e

: "${MODEL_NAME:?MODEL_NAME é obrigatória}"
: "${AGENT_ADMIN_SECRET:?AGENT_ADMIN_SECRET é obrigatória}"

VLLM_PORT="${VLLM_PORT:-8001}"
AGENT_PORT="${AGENT_PORT:-8000}"
VLLM_LOG_FILE="${VLLM_LOG_FILE:-/var/log/vllm.log}"

echo "[entrypoint] subindo vLLM com modelo ${MODEL_NAME} na porta ${VLLM_PORT}"
python3 -m vllm.entrypoints.openai.api_server \
  --model "${MODEL_NAME}" \
  --host 127.0.0.1 \
  --port "${VLLM_PORT}" \
  ${VLLM_EXTRA_ARGS:-} \
  >"${VLLM_LOG_FILE}" 2>&1 &

echo "[entrypoint] subindo agent na porta ${AGENT_PORT}"
exec uvicorn main:app \
  --app-dir /opt/agent \
  --host 0.0.0.0 \
  --port "${AGENT_PORT}"
