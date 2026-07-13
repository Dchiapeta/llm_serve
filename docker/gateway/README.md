# Gateway estável de inferência

O único endpoint público que o cliente final conhece. Recebe a chamada com a
chave HEX, resolve em qual máquina está o adapter LoRA da conta
(`routing_state` no Supabase) e faz o proxy — incluindo streaming SSE — para
o agent daquele pod. Quando o cliente migra de máquina, a URL não muda.

```
cliente → gateway (:8080) → agent do pod (:8000) → vLLM (:8001)
```

## Comportamento

- **Auth**: `Authorization: Bearer <chave-hex>` → SHA-256 → `api_keys` no
  Supabase, com cache em memória (`KEY_CACHE_TTL_S`, default 60s; cache
  negativo de 5s). O painel chama `POST /admin/flush-key-cache` ao revogar.
- **Roteamento**: rota com `machine_id` e status `loaded`/`migrating` → proxy
  direto (durante migração a origem continua servindo até o flip). `loading`
  → espera curta (`LOAD_WAIT_TIMEOUT_S`, default 20s) e 503 + `Retry-After`
  se não resolver.
- **Alocação (placeholder)**: primeira máquina `running` com menos de
  `MAX_LORAS_PER_MACHINE` rotas ativas; claim atômico (`claim_route`);
  upsert da chave no agent; download+load do adapter; rota confirmada como
  `loaded`. Falha no load → slot liberado + 503. A Fase 6 troca o teto fixo
  pelo cálculo por VRAM.
- **Conta sem adapter registrado**: roteia para o modelo base (sem reescrever
  `model`) na primeira máquina running.
- **Proxy**: reescreve `body.model = "acct-{account_id}"` quando o adapter
  está ativo e repassa a Bearer original — o agent continua validando e
  contando uso por chave. Máquina fora do ar → 503 imediato (connect 5s).

## Limitação aceita: réplica única

O contador de requests in-flight (usado no drain da migração, Fase 5) e o
idle reaper vivem **em memória do processo**. Com 2+ réplicas, cada uma
enxergaria só os próprios streams e uma migração poderia cortar respostas no
meio. Decisão de MVP registrada em plano: rodar 1 réplica. Quando escalar,
externalizar o in-flight para Postgres e mover o reaper para pg_cron +
advisory lock.

## Variáveis de ambiente

| Variável                  | Obrigatória | Descrição                                        |
| ------------------------- | ----------- | ------------------------------------------------ |
| `SUPABASE_URL`            | sim         | URL do projeto Supabase                          |
| `SUPABASE_SERVICE_ROLE_KEY` | sim       | Service role key (PostgREST + Storage)           |
| `GATEWAY_ADMIN_SECRET`    | sim         | Protege as rotas `/admin/*`                      |
| `GATEWAY_PORT`            | não         | Porta HTTP (default 8080)                        |
| `KEY_CACHE_TTL_S`         | não         | TTL do cache de chaves (default 60)              |
| `LORA_LOAD_TIMEOUT_S`     | não         | Timeout do load de adapter (default 120)         |
| `LORA_BUCKET`             | não         | Bucket dos adapters (default `loras`)            |
| `MAX_LORAS_PER_MACHINE`   | não         | Teto de adapters por máquina (default 8)         |
| `LOAD_WAIT_TIMEOUT_S`     | não         | Espera por load de outro request (default 20)    |
| `IDLE_UNLOAD_MINUTES`     | não         | Ociosidade até o unload do adapter (default 30; 0 desliga o reaper) |
| `MIGRATION_DRAIN_TIMEOUT_S` | não       | Espera máx. pelos streams da origem na migração (default 600) |

## Rodar local

```bash
cd docker/gateway
pip install -r requirements.txt
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... GATEWAY_ADMIN_SECRET=dev \
  uvicorn main:app --port 8080
```

## Build e deploy

```bash
cd docker/gateway
docker buildx build --platform linux/amd64 -t SEU_USUARIO/llm-gateway:latest --push .
```

Rode em qualquer host com CPU (pod CPU da RunPod, VPS, etc.) — precisa
alcançar o Supabase e os proxies `*.proxy.runpod.net` dos pods.

## Endpoints

- `POST /v1/...` — proxy OpenAI-compatible (Bearer HEX do cliente)
- `GET /` / `GET /health` — health checks
- `GET /admin/routes` — in-flight e tamanho do cache (header `X-Admin-Secret`)
- `POST /admin/flush-key-cache` — invalida o cache de chaves (revogação imediata)
- `POST /admin/migrate` — `{account_id, target_machine_id}`: migra o adapter
  sem perder request (migrating → load no destino → flip → drain → unload)
- `POST /admin/reap-idle` — dispara um ciclo do idle reaper (útil em teste)

## Lifecycle

- **Idle reaper** (task asyncio, a cada 60s): rotas `loaded` sem uso além de
  `IDLE_UNLOAD_MINUTES` e sem request em voo → unload no agent + slot livre.
  Falha no unload mantém `loaded` e tenta no próximo ciclo. O request
  seguinte da conta recarrega o adapter de forma transparente.
- **Migração ativa**: nunca corta um stream — a origem continua servindo
  durante todo o load no destino (o roteamento segue o `machine_id`); o flip
  só acontece com o load confirmado, e o unload da origem espera os requests
  em voo terminarem (`MIGRATION_DRAIN_TIMEOUT_S`). Teste de não-perda:
  [scripts/test-migration.py](../../scripts/test-migration.py).
