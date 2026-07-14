#!/bin/bash
set -e

: "${MODEL_NAME:?MODEL_NAME é obrigatória}"
: "${AGENT_ADMIN_SECRET:?AGENT_ADMIN_SECRET é obrigatória}"

VLLM_PORT="${VLLM_PORT:-8001}"
AGENT_PORT="${AGENT_PORT:-8000}"
VLLM_LOG_FILE="${VLLM_LOG_FILE:-/var/log/vllm.log}"

# GPU_COUNT é injetada pelo painel a partir de templates.gpu_count. >1 liga
# tensor parallelism automaticamente — sem isso o vLLM só enxergava a GPU 0
# mesmo em pods com múltiplas GPUs.
GPU_COUNT="${GPU_COUNT:-1}"
TP_ARGS=""
if [ "${GPU_COUNT}" -gt 1 ] 2>/dev/null; then
  TP_ARGS="--tensor-parallel-size ${GPU_COUNT}"
  echo "[entrypoint] tensor parallelism habilitado (${TP_ARGS})"
fi

# Multi-LoRA dinâmico (opt-in): ENABLE_LORA=true habilita adapters carregados
# em runtime via /v1/load_lora_adapter, sem reiniciar o pod.
ENABLE_LORA="${ENABLE_LORA:-false}"
LORA_ARGS=""
if [ "${ENABLE_LORA}" = "true" ]; then
  # exigido pelo vLLM para expor os endpoints de load/unload dinâmico
  export VLLM_ALLOW_RUNTIME_LORA_UPDATING=True
  LORA_ARGS="--enable-lora --max-loras ${MAX_LORAS:-8} --max-lora-rank ${MAX_LORA_RANK:-64}"
  echo "[entrypoint] multi-LoRA habilitado (${LORA_ARGS})"
fi

echo "[entrypoint] subindo vLLM com modelo ${MODEL_NAME} na porta ${VLLM_PORT}"
# output do vLLM vai pro stdout do container (visível no RunPod) E pro arquivo
# que o agent lê em /admin/logs.
python3 -m vllm.entrypoints.openai.api_server \
  --model "${MODEL_NAME}" \
  --host 127.0.0.1 \
  --port "${VLLM_PORT}" \
  ${TP_ARGS} \
  ${LORA_ARGS} \
  ${VLLM_EXTRA_ARGS:-} \
  2>&1 | sed -u 's/^/[vllm] /' | tee "${VLLM_LOG_FILE}" &

echo "[entrypoint] subindo agent na porta ${AGENT_PORT}"
exec uvicorn main:app \
  --app-dir /opt/agent \
  --host 0.0.0.0 \
  --port "${AGENT_PORT}"
