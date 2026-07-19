# Claude Code nos planos VibeCoder e Pro (janela 64k)

Por que existe: o Claude Code CLI manda ~26k tokens só de system prompt +
tool schemas antes de qualquer trabalho — numa janela de 16k ele nem abre
sessão (era o 400 cru do vLLM: "maximum context length is 16384 tokens...")
e em 32k compacta o tempo todo. Com 64k ele funciona. Este doc cobre a
config nova dos templates dos DOIS planos, o gate de load test e o setup do
usuário.

Pré-requisitos já no código (deploy antes de subir os pods novos):
- Migration `0031_machine_max_model_len.sql` — gateway passa a conhecer a
  janela e clampa `max_tokens` ao orçamento restante (erro claro em vez do
  400 críptico).
- Migration `0032_stack_usage_class.sql` — ocupação ponderada por classe de
  uso: usuários de contexto longo pesam mais na máquina (protege os demais).

## 1. Ordem de deploy (ESTRITA — não inverter)

**Migrations 0031 e 0032 no Supabase ANTES do push na main.** O gateway
tolera DB não migrado (fallbacks), mas o painel NÃO: sem as colunas/RPC,
criar máquina falha 100% (insert com `max_model_len` rejeitado → pod órfão
deletado), `migrateStack` quebra ("Stack não encontrada") e a validação de
lotação enxerga toda máquina como vazia (rpc ausente → ocupação 0 →
overcommit silencioso). O inverso (DB migrado, código antigo) é seguro.

## 2. Config dos templates (painel → Templates)

Regras comuns aos dois planos:
- **NÃO ligar prefix caching**: pods compartilhados rodam com
  `DISABLE_PREFIX_CACHING=true` de propósito (canal lateral de timing entre
  co-tenants — ver lib/types.ts SHARED_POD_PLANS). Não reverter.
- `kv_reserve_gb_per_user` = a unidade de um usuário **low** (peso 1.0);
  um high custa 3× isso na ocupação.

### VibeCoder (A40 48GB, Qwen3.5-9B)

```
--max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 8
```

- Se a janela NATIVA do modelo for menor que 65536 (conferir
  `max_position_embeddings` no config.json do HF), estender com YaRN via
  `--hf-overrides` (o vLLM 0.24 removeu `--rope-scaling`):
  `--hf-overrides {"rope_scaling":{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}}`
- Orçamento: ~19 GB de pesos → ~22-24 GB de KV ≈ **~300k tokens fp8** ≈
  ~4 sessões cheias de 64k + usuários leves.
- `--max-num-seqs 8`: ponto de partida; calibrar no load test.

### Pro (L40S 48GB, Qwen3.6-27B-FP8)

```
--max-model-len 65536 --kv-cache-dtype fp8 --max-num-seqs 16
```

- Janela nativa do 27B é 262k — **sem YaRN/hf-overrides**, só a flag.
- `--kv-cache-dtype fp8` o Pro já usa — conferir que permanece.
- `--max-num-seqs 16` foi o mínimo pro pod SUBIR (histórico de OOM no boot);
  com sequências 2× maiores, o load test decide se reduz.
- Orçamento: ~28 GB de pesos → pool de KV **~13 GB ≈ ~130k tokens fp8** ≈
  **~2 sessões cheias de 64k simultâneas** + usuários leves. É apertado de
  propósito (performance-first): quem impede a máquina de aceitar mais
  usuários pesados do que cabe são as classes de uso (high pesa 3.0 slots).
- Calibrar `kv_reserve_gb_per_user` do template para que high (3×) ≈
  6-6.5 GB de KV (uma sessão de 64k no 27B ≈ ~100KB/token fp8). Ex.:
  reserva 2 GB com footprint 28 → orçamento (48−28)/2 = 10 slots; 2 highs
  (6.0) + 4 lows (4.0) lotam — coerente com o pool real.
- **128k no Pro foi avaliado e ADIADO**: uma sessão de 128k consumiria o
  pool inteiro da L40S. Exige A100 80GB (~3 sessões cheias) ou 2× L40S
  TP=2 (~4 sessões) — decisão de custo pendente.

## 3. Aplicação (checklist por template — NÃO pular)

1. Editar o template no painel.
2. **Conferir no console do RunPod que o template refletiu a mudança** —
   precedente real: o `updateTemplate` já falhou silenciosamente e o RunPod
   ficou com a config antiga enquanto o DB mostrava a nova (aconteceu no
   Pro: DB 32K, pod subindo com 16K).
3. Subir o pod novo e conferir no log de boot do vLLM:
   `max_model_len=65536` e `kv_cache_dtype=fp8`.
4. Conferir o backfill: `select name, max_model_len from machines;`
   (máquinas novas = 65536; as antigas mantêm o valor de criação até serem
   recriadas — NÃO fazer UPDATE manual da coluna sem recriar o pod, o
   gateway passaria a clampar com uma janela que o pod não tem).

## 4. Load test (gate — a mudança só vale se passar)

Payloads estilo Claude Code = contexto sintético grande + streaming.
Manter o input sintético ≤ ~50k: o gateway agora reserva a estimativa do
prompt ANTES do vLLM, então input perto da janela gera 400 do próprio clamp
(esperado, não é erro do teste).

```bash
# VibeCoder (~4 sessões cheias de folga)
python3 scripts/loadtest.py \
  --base-url https://llmserve-docker.up.railway.app \
  --api-key <chave VibeCoder> --model <alias vibecoder> \
  --levels 4,6,10 --context-tokens 30000 --max-tokens 16000

# Pro (pool menor: concorrência pesada mais baixa)
python3 scripts/loadtest.py \
  --base-url https://llmserve-docker.up.railway.app \
  --api-key <chave Pro> --model <alias pro> \
  --levels 2,4,6 --context-tokens 30000 --max-tokens 16000
```

E uma rodada de regressão em cada plano com as tarefas normais (sem
`--context-tokens`), igual à validação do template v2 (70/70).

Critérios de aprovação (por plano):
- zero 400 de contexto e zero OOM/crash do vLLM;
- TTFT aceitável no pico (referência: Pro ~10s em carga pesada);
- regressão das tarefas normais sem degradação relevante.

Se degradar: reduzir `--max-num-seqs`; último recurso, janela 49152 (ainda
comporta os ~26k fixos do Claude Code + trabalho útil).

Cuidado ao testar direto no pod (bypass do gateway): o idle-reaper não vê
atividade e pode pausar a máquina no meio do teste — preferir sempre o
gateway.

## 5. Setup do usuário (onboarding — igual nos dois planos)

```bash
export ANTHROPIC_BASE_URL=https://llmserve-docker.up.railway.app
export ANTHROPIC_AUTH_TOKEN=<sua chave do plano>
export ANTHROPIC_MODEL=<alias do modelo do plano>
# a janela real é 64k — sem isso o Claude Code assume 200k e só descobre o
# limite quando o gateway recusa; com isso ele compacta sozinho antes
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=50000
```

Notas para o usuário:
- Não existe config oficial de "tamanho de janela" no Claude Code para
  endpoints customizados (ele assume 200k); `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  é o mecanismo suportado para compactar antes do limite real.
- O gateway responde com erro claro quando o contexto estoura, com instrução
  de usar `/compact`.
- Boas práticas na janela de 64k: CLAUDE.md do projeto enxuto, usar
  subagents para pesquisa longa (contexto zerado), `/compact` manual em
  sessões longas.

## 6. O que protege os outros usuários

- Curto prazo (picos): `--max-num-seqs`, preempção do vLLM e a concorrência
  elástica do gateway (`check_concurrency`).
- Médio prazo: classificação de consumo (0032) — o loop do gateway
  reclassifica stacks pelo uso real (janela de 14 dias, mínimo 5 dias
  ativos, cooldown de 7 dias) e a alocação ponderada impede concentrar
  usuários high na mesma máquina. A classe só muda alocação em eventos
  naturais de realocação; ninguém é migrado no meio do trabalho.
- Limiares iniciais são chutes razoáveis — calibrar com a distribuição real
  de `usage_metrics` após ~2 semanas (override sem deploy em
  `templates.usage_class_config`). Os limiares de tokens/request derivam
  sozinhos da janela nova (fração de 64k); os diários (VibeCoder 300k/1.5M,
  Pro 600k/3M por dia) são os candidatos à calibração.
