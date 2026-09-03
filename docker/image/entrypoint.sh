#!/bin/bash
set -e

# Entrypoint do pod de GERAÇÃO DE IMAGEM. Mesma estrutura do docker/entrypoint.sh
# (vLLM), com um processo de inferência diferente na 8001:
#
#   servidor diffusers (8001, só 127.0.0.1)  +  agent de auth/telemetria (8000)
#
# O agent é o MESMO binário dos pods de LLM (docker/agent/main.py) — é ele que
# valida a chave, conta uso e expõe /admin/*. Só o que roda atrás dele muda.

: "${MODEL_NAME:?MODEL_NAME é obrigatória}"
: "${AGENT_ADMIN_SECRET:?AGENT_ADMIN_SECRET é obrigatória}"

IMAGE_PORT="${IMAGE_PORT:-8001}"
AGENT_PORT="${AGENT_PORT:-8000}"
# Mesmo nome de variável do pod de vLLM: é o arquivo que o /admin/logs do agent
# lê (docker/agent/main.py:VLLM_LOG_FILE). Manter o nome é o que faz aquela rota
# funcionar sem mudança.
VLLM_LOG_FILE="${VLLM_LOG_FILE:-/var/log/vllm.log}"

# Agulha que o /health do agent procura em /proc/*/cmdline para distinguir
# "ainda carregando" de "processo morreu" (docker/agent/main.py:_vllm_process_alive).
#
# TEM que casar LITERALMENTE com a linha de comando abaixo. O default do agent é
# a string do vLLM, que nunca apareceria aqui: sem esta variável, `vllm_alive`
# ficaria false durante todo o boot e o painel mostraria "Falha" num pod
# perfeitamente saudável que só está baixando 16 GB de pesos.
#
# Nem o `sed`, nem o `tee`, nem o próprio entrypoint.sh contêm este caminho —
# então não há falso positivo de um processo auxiliar sobrevivendo à morte do
# servidor.
export SERVER_PROCESS_MATCH="${SERVER_PROCESS_MATCH:-/opt/agent/server.py}"

# HF_HOME vem do env do template, apontando para o volume (/models/huggingface).
# O container disk do RunPod é reconstruído a cada start, então sem o volume o
# pod rebaixaria ~16 GB toda vez que a auto-pausa despausasse — não só na
# recriação. O aviso existe porque um template sem essa variável BOOTA normal e
# o custo só aparece na segunda vez.
if [ -z "${HF_HOME:-}" ]; then
  echo "[entrypoint] AVISO: HF_HOME não definida — os pesos vão para o container disk e serão rebaixados a cada start"
fi

echo "[entrypoint] subindo servidor de imagem (${MODEL_NAME}) na porta ${IMAGE_PORT}"
echo "[entrypoint] revision=${IMAGE_MODEL_REVISION:-main} dtype=${IMAGE_DTYPE:-bfloat16} steps=${IMAGE_STEPS:-4}"
echo "[entrypoint] fila: capacity=${IMAGE_QUEUE_CAPACITY:-4} wait_timeout=${IMAGE_QUEUE_WAIT_TIMEOUT_S:-60}s warmup=${IMAGE_WARMUP_RUNS:-2}"

# stdout do servidor vai para o stdout do container (visível no RunPod) E para o
# arquivo que o agent serve em /admin/logs.
IMAGE_PORT="${IMAGE_PORT}" python3 /opt/agent/server.py \
  2>&1 | sed -u 's/^/[image] /' | tee "${VLLM_LOG_FILE}" &

echo "[entrypoint] subindo agent na porta ${AGENT_PORT}"
exec uvicorn main:app \
  --app-dir /opt/agent \
  --host 0.0.0.0 \
  --port "${AGENT_PORT}"
