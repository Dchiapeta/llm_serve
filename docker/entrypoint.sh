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

# Tool-calling nativo (opt-in): o Codex CLI fala só a Responses API
# (/v1/responses) e depende do vLLM parsear tool calls e reasoning
# nativamente — sem isso o raciocínio vaza pro campo "content" (por isso o
# filtro <think> do gateway existe hoje) e chamadas de ferramenta não
# funcionam. Desligado por padrão pra não mudar templates já em produção;
# ligar por template via ENABLE_TOOL_CALLING=true + os parsers certos pro
# modelo (ex.: Qwen3.x -> TOOL_CALL_PARSER=qwen3_coder REASONING_PARSER=qwen3
# — ver docs.vllm.ai/serving/integrations/codex). Ligar isso torna o filtro
# <think> do gateway redundante pra esse template (reasoning já vem separado
# em "reasoning_content", não mais em "content") — reconciliar depois.
ENABLE_TOOL_CALLING="${ENABLE_TOOL_CALLING:-false}"
TOOL_CALLING_ARGS=""
if [ "${ENABLE_TOOL_CALLING}" = "true" ]; then
  : "${TOOL_CALL_PARSER:?TOOL_CALL_PARSER é obrigatória quando ENABLE_TOOL_CALLING=true}"
  : "${REASONING_PARSER:?REASONING_PARSER é obrigatória quando ENABLE_TOOL_CALLING=true}"
  TOOL_CALLING_ARGS="--enable-auto-tool-choice --tool-call-parser ${TOOL_CALL_PARSER} --reasoning-parser ${REASONING_PARSER}"
  echo "[entrypoint] tool-calling habilitado (${TOOL_CALLING_ARGS})"
fi

# Prefix caching automático (opt-in pra DESLIGAR — vLLM liga por padrão em
# versões recentes): em pod COMPARTILHADO entre tenants (várias stacks/
# contas no mesmo processo vLLM), um cache hit de prefixo (prompt com o
# mesmo início de outro tenant) reduz o TTFT de forma observável — um canal
# lateral de tempo que pode vazar informação sobre o prompt de outro
# tenant por inferência. Planos de pod DEDICADO não têm esse problema (sem
# co-tenant pra inferir nada) e não precisam desta flag. Desligar por
# template de pod compartilhado via DISABLE_PREFIX_CACHING=true. Flag
# exata depende da versão do vLLM em produção (BooleanOptionalAction:
# --no-enable-prefix-caching nas versões recentes) — validar antes de
# ligar em produção.
DISABLE_PREFIX_CACHING="${DISABLE_PREFIX_CACHING:-false}"
PREFIX_CACHING_ARGS=""
if [ "${DISABLE_PREFIX_CACHING}" = "true" ]; then
  PREFIX_CACHING_ARGS="--no-enable-prefix-caching"
  echo "[entrypoint] prefix caching desligado (pod compartilhado entre tenants)"
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
  ${TOOL_CALLING_ARGS} \
  ${PREFIX_CACHING_ARGS} \
  ${VLLM_EXTRA_ARGS:-} \
  2>&1 | sed -u 's/^/[vllm] /' | tee "${VLLM_LOG_FILE}" &

echo "[entrypoint] subindo agent na porta ${AGENT_PORT}"
exec uvicorn main:app \
  --app-dir /opt/agent \
  --host 0.0.0.0 \
  --port "${AGENT_PORT}"
