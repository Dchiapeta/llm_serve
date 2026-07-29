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
# (/v1/responses) e depende do vLLM parsear tool calls nativamente. Ligar
# por template via ENABLE_TOOL_CALLING=true + TOOL_CALL_PARSER (ex.:
# qwen3_coder — ver docs.vllm.ai/serving/integrations/codex).
ENABLE_TOOL_CALLING="${ENABLE_TOOL_CALLING:-false}"
TOOL_CALLING_ARGS=""
if [ "${ENABLE_TOOL_CALLING}" = "true" ]; then
  : "${TOOL_CALL_PARSER:?TOOL_CALL_PARSER é obrigatória quando ENABLE_TOOL_CALLING=true}"
  TOOL_CALLING_ARGS="--enable-auto-tool-choice --tool-call-parser ${TOOL_CALL_PARSER}"
  echo "[entrypoint] tool-calling habilitado (${TOOL_CALLING_ARGS})"
fi

# Reasoning-parser nativo (opt-in, independente do tool-calling acima):
# modelos com "thinking" ligado (ex.: Qwen3.x) emitem o raciocínio (até
# </think>) misturado no campo "content" quando essa flag está desligada —
# por isso o filtro <think> do gateway existe hoje (REASONING_LEAK_PLANS em
# docker/gateway/main.py). Ligar via ENABLE_REASONING_PARSER=true +
# REASONING_PARSER (ex.: qwen3) faz o vLLM separar o raciocínio em
# "reasoning_content", o que o gateway já detecta e usa para não represar
# a resposta inteira até o fim do stream.
ENABLE_REASONING_PARSER="${ENABLE_REASONING_PARSER:-false}"
REASONING_PARSER_ARGS=""
if [ "${ENABLE_REASONING_PARSER}" = "true" ]; then
  : "${REASONING_PARSER:?REASONING_PARSER é obrigatória quando ENABLE_REASONING_PARSER=true}"
  REASONING_PARSER_ARGS="--reasoning-parser ${REASONING_PARSER}"
  echo "[entrypoint] reasoning-parser habilitado (${REASONING_PARSER_ARGS})"
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
  ${REASONING_PARSER_ARGS} \
  ${PREFIX_CACHING_ARGS} \
  ${VLLM_EXTRA_ARGS:-} \
  2>&1 | sed -u 's/^/[vllm] /' | tee "${VLLM_LOG_FILE}" &

echo "[entrypoint] subindo agent na porta ${AGENT_PORT}"
exec uvicorn main:app \
  --app-dir /opt/agent \
  --host 0.0.0.0 \
  --port "${AGENT_PORT}"
