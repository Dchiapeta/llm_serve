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

# Prefix caching, controlado por um contrato de DUAS variáveis.
#
# O problema: em pod COMPARTILHADO (várias stacks/contas no mesmo processo
# vLLM), um cache hit de prefixo reduz o TTFT de forma observável — um tenant
# mede o próprio tempo de resposta e infere se um prefixo já foi processado
# por outro. Canal lateral de tempo. Por isso o caching ficou desligado nesses
# planos via DISABLE_PREFIX_CACHING=true. O custo era severo: cliente agêntico
# manda ~85k tokens por turno e reprocessava TUDO a cada mensagem.
#
# A solução: PREFIX_CACHE_ISOLATION=cache_salt liga o caching E prova, na mesma
# variável, que o agent desta imagem sabe isolar (ele injeta um cache_salt por
# stack em toda request — ver docker/agent/proxy_policy.py). As duas coisas
# viajam no mesmo container, então nenhuma ordem de deploy consegue ligar o
# caching sem o isolamento:
#
#   imagem velha + var no template -> shell antigo ignora a var  -> off, seguro
#   imagem nova  + var ausente     -> nada liga                  -> off, seguro
#   imagem nova  + var no template -> caching ON, salt ON        -> objetivo
#
# DISABLE_PREFIX_CACHING=true continua sendo injetado pelo painel em todo pod
# compartilhado (lib/actions.ts) e continua sendo o padrão fail-closed: se a
# variável nova sumir do template por engano, o pod volta pro comportamento
# lento e seguro em vez de ficar rápido e vazando.
#
# --enable-prefix-caching EXPLÍCITO é obrigatório, não redundante: modelos
# híbridos (Qwen3.5/3.6, linear + full attention) têm attn_type == "hybrid", e
# em vllm/config/model.py:is_prefix_caching_supported o default para esses é
# DESLIGADO ("the feature is still experimental"). Só tirar o --no- deixaria o
# caching off em silêncio. O flag explícito não é sobrescrito para modelo
# generativo (o aviso em arg_utils.py só vale pra runner_type == "pooling").
# Não passar --mamba-block-size: com caching ligado o vLLM alinha sozinho ao
# block_size, e passá-lo num pod SEM caching é erro fatal de boot.
#
# Canal residual aceito conscientemente: todos os tenants disputam o mesmo pool
# LRU, então a variância do próprio hit rate ainda dá um sinal grosseiro de
# VOLUME de atividade dos vizinhos — sem conteúdo, e em parte já presente hoje
# via contenção de fila.
PREFIX_CACHE_ISOLATION="${PREFIX_CACHE_ISOLATION:-}"
DISABLE_PREFIX_CACHING="${DISABLE_PREFIX_CACHING:-false}"
PREFIX_CACHING_ARGS=""
if [ "${PREFIX_CACHE_ISOLATION}" = "cache_salt" ]; then
  : "${AGENT_ADMIN_SECRET:?AGENT_ADMIN_SECRET é obrigatória com PREFIX_CACHE_ISOLATION=cache_salt}"
  PREFIX_CACHING_ARGS="--enable-prefix-caching"
  echo "[entrypoint] prefix caching LIGADO, isolado por cache_salt (salt por stack, injetado pelo agent)"
elif [ "${DISABLE_PREFIX_CACHING}" = "true" ]; then
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
