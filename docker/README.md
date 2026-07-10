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
| `ENABLE_LORA`        | não         | `true` habilita multi-LoRA dinâmico (default `false`) |
| `MAX_LORAS`          | não         | Máx. de adapters simultâneos em VRAM (default 8) |
| `MAX_LORA_RANK`      | não         | Rank máximo aceito nos adapters (default 64; o vLLM 0.24 só aceita 1, 8, 16, 32, 64, 128, 256, 320 ou 512) |
| `LORA_DIR`           | não         | Diretório local dos adapters baixados (default `/workspace/loras`) |

O painel injeta `MODEL_NAME` e `AGENT_ADMIN_SECRET` automaticamente ao criar a
máquina a partir de um template. Com `ENABLE_LORA=true`, o entrypoint exporta
`VLLM_ALLOW_RUNTIME_LORA_UPDATING=True` e sobe o vLLM com
`--enable-lora --max-loras --max-lora-rank`, permitindo carregar/descarregar
adapters em runtime sem reiniciar o pod.

## Endpoints do agent

- `POST /v1/...` — proxy OpenAI-compatible (exige `Authorization: Bearer <chave-hex>`)
- `GET /health` — health check público
- `POST /admin/sync-keys` — recebe hashes das chaves ativas (header `X-Admin-Secret`)
- `POST /admin/upsert-keys` — insere/atualiza chaves sem limpar as existentes (usado pelo gateway)
- `GET /admin/metrics` — uso agregado e por chave
- `GET /admin/logs?key_prefix=&tail=` — logs da máquina (vLLM) ou por usuário
- `POST /admin/load-lora` — baixa o adapter (signed URLs) e carrega no vLLM
- `POST /admin/unload-lora` — descarrega o adapter e apaga os arquivos locais
- `GET /admin/loras` — lista adapters carregados no vLLM
