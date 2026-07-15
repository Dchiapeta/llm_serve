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
  `model`) na primeira máquina running **do template compatível com o plano
  da conta** (`accounts.plan` × `templates.plan`) — necessário desde que
  existe mais de um modelo base em produção (VibeCoder × Pro/Max).
- **Proxy**: reescreve `body.model = "acct-{account_id}"` quando o adapter
  está ativo e repassa a Bearer original — o agent continua validando e
  contando uso por chave. Máquina fora do ar → 503 imediato (connect 5s).
- **System prompt + RAG**: em chat completions, injeta o `system_prompt`
  configurado da conta como primeira mensagem e, se a conta tiver arquivos
  indexados na base de conhecimento, embeda a última mensagem do usuário
  (OpenAI `text-embedding-3-small`) e injeta o top-k mais similar
  (`match_knowledge_chunks`) como contexto antes da mensagem do usuário.
  Best-effort: falha na API de embeddings não derruba o request.

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
| `IDLE_RELEASE_MINUTES`    | não         | Ociosidade até liberar o slot (unload + rota livre; default 60; 0 desliga o reaper). Fallback: `IDLE_UNLOAD_MINUTES` |
| `MIGRATION_DRAIN_TIMEOUT_S` | não       | Espera máx. pelos streams da origem na migração (default 600) |
| `RUNPOD_API_KEY`          | não*        | API key do RunPod — sem ela, a auto-pausa e o auto-wake de máquinas ficam desligados (warning no boot) |
| `MACHINE_IDLE_STOP_MINUTES` | não       | Máquina sem nenhuma atividade por esse tempo (e sem rotas) → stopPod (default 60; 0 desliga) |
| `WAKE_COOLDOWN_S`         | não         | Intervalo mínimo entre tentativas de startPod na mesma máquina (default 120) |
| `CONSOLIDATION_INTERVAL_S` | não        | Intervalo do loop de consolidação + auto-pausa (default 300) |
| `CONSOLIDATION_MAX_ORIGIN_ROUTES` | não | Máx. de rotas para uma máquina ser candidata a esvaziar (default 2) |
| `STOP_RECHECK_GRACE_S`    | não         | Grace entre o flip `stopped` e o stopPod, para re-checar claims (default 5) |
| `OPENAI_API_KEY`          | não*        | Embeddings do RAG — sem ela, a injeção de contexto é pulada (best-effort) |
| `EMBEDDING_MODEL`         | não         | Modelo de embedding (default `text-embedding-3-small`, precisa bater com a indexação do painel) |
| `RAG_TOP_K`               | não         | Quantidade de chunks injetados como contexto (default 4)       |

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
- `POST /admin/consolidate` — dispara um ciclo de consolidação (útil em teste)
- `POST /admin/stop-idle-machines` — dispara um ciclo de auto-pausa (útil em teste)

## Lifecycle

- **Idle reaper** (task asyncio, a cada 60s): rotas `loaded` sem uso além de
  `IDLE_RELEASE_MINUTES` e sem request em voo → unload no agent + slot livre
  (rota volta a `unloaded` com `machine_id` nulo). Falha no unload mantém
  `loaded` e tenta no próximo ciclo. O request seguinte da conta realoca em
  qualquer máquina com vaga, de forma transparente.
- **Migração ativa**: nunca corta um stream — a origem continua servindo
  durante todo o load no destino (o roteamento segue o `machine_id`); o flip
  só acontece com o load confirmado, e o unload da origem espera os requests
  em voo terminarem (`MIGRATION_DRAIN_TIMEOUT_S`). Teste de não-perda:
  [scripts/test-migration.py](../../scripts/test-migration.py).
- **Consolidação** (task asyncio, a cada `CONSOLIDATION_INTERVAL_S`): máquina
  running com poucas rotas (`1..CONSOLIDATION_MAX_ORIGIN_ROUTES`, todas
  `loaded` e sem request em voo) e outra máquina do MESMO template com vagas
  para todas → migra conta a conta (com o drain acima) para a máquina mais
  cheia que caiba. Ex.: A=16, B=1 → A=17, B=0. Máx. 1 máquina-origem por
  ciclo. A origem esvaziada pausa depois pela regra abaixo.
- **Reconciliação de status** (início de cada ciclo do lifecycle): alinha
  `machines.status` com o `desiredStatus` real dos pods no RunPod (RUNNING →
  running, EXITED → stopped, TERMINATED → terminated), espelho do
  `reconcileMachineStatuses` do painel — que só roda quando alguém abre uma
  página. Sem isso, máquina recém-criada fica `creating` no banco para sempre
  e a auto-pausa nunca a enxerga, mesmo com o pod cobrando GPU. Em qualquer
  promoção a `running`, o `last_activity_at` é tocado: o relógio de ociosidade
  conta a partir de quando a máquina ficou DE PÉ — sem isso, uma máquina
  religada com atividade velha seria re-pausada no ciclo seguinte (o
  `startMachine` do painel também toca, pois lá o flip não passa por aqui).
- **Auto-pausa**: máquina running sem nenhuma atividade
  (`machines.last_activity_at`, tocada a cada request proxied) há
  `MACHINE_IDLE_STOP_MINUTES`, sem rotas ativas e sem request em voo →
  `machines.status='stopped'` no banco (novos claims param de enxergá-la) →
  grace de `STOP_RECHECK_GRACE_S` + re-checagem (claim que escapou → revert
  para `running`) → stopPod na API RunPod (falha → revert + retry no próximo
  ciclo). Requests de contas cuja máquina pausou alocam em qualquer máquina
  running com vaga; sem nenhuma vaga, entra o auto-wake abaixo.
  - Janela residual: um claim que passe depois da re-checagem cria uma rota
    `loading` para a máquina pausada; o load falha (503), o slot é liberado
    e o retry do cliente aloca em outra máquina — degradação auto-corretiva.
- **Auto-wake**: request chega e NENHUMA máquina running do template do plano
  tem slot livre → o gateway religa (startPod) a máquina `stopped` mais antiga
  do plano que aceitar o start e responde `503` + `Retry-After: 60` ("máquina
  religando"). O `last_activity_at` é tocado antes do flip para `running` —
  senão a auto-pausa pararia a máquina de novo durante o warm-up do vLLM
  (~3–8 min); nesse warm-up, retries recebem 503 "máquina indisponível" até o
  agent responder, e então a alocação + load do adapter seguem o fluxo normal.
  `WAKE_COOLDOWN_S` impede tempestade de startPod (requests concorrentes ou
  host sem GPU livre — nesse caso tenta as demais pausadas; nenhuma subiu →
  503 simples, recuperação via `recreateMachine` no painel). Rota que ainda
  aponta para máquina parada manualmente (stop pelo painel) é liberada
  (`mark_slot_idle`) e a conta realoca — o restart zera a VRAM de qualquer
  forma. Religar manualmente pelo painel continua funcionando igual.
