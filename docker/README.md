# Imagem Docker — vLLM + Agent

Imagem usada nos pods do RunPod: o vLLM roda internamente na porta 8001 e o
agent (FastAPI) fica na porta 8000 — a única exposta. O agent valida as chaves
HEX dos usuários, contabiliza uso por chave e expõe métricas/logs ao painel.

## Build e push

```bash
cd docker
docker build -t SEU_USUARIO/vllm-agent:latest .
docker push SEU_USUARIO/vllm-agent:latest
```

> Em Mac com Apple Silicon, faça o build para a arquitetura dos pods (amd64):
> `docker buildx build --platform linux/amd64 -t SEU_USUARIO/vllm-agent:latest --push .`

## Variáveis de ambiente

| Variável             | Obrigatória | Descrição                                    |
| -------------------- | ----------- | -------------------------------------------- |
| `MODEL_NAME`         | sim         | Modelo HF servido pelo vLLM                  |
| `AGENT_ADMIN_SECRET` | sim         | Secret que o painel usa nas rotas `/admin/*` |
| `VLLM_EXTRA_ARGS`    | não         | Args extras do vLLM (ex: `--max-model-len 8192`) |
| `HF_TOKEN`           | não         | Token HuggingFace para modelos gated         |

O painel injeta `MODEL_NAME` e `AGENT_ADMIN_SECRET` automaticamente ao criar a
máquina a partir de um template.

## Endpoints do agent

- `POST /v1/...` — proxy OpenAI-compatible (exige `Authorization: Bearer <chave-hex>`)
- `GET /health` — health check público
- `POST /admin/sync-keys` — recebe hashes das chaves ativas (header `X-Admin-Secret`)
- `GET /admin/metrics` — uso agregado e por chave
- `GET /admin/logs?key_prefix=&tail=` — logs da máquina (vLLM) ou por usuário
