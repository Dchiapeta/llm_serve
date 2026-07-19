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

**STATUS: aplicado em 19/07/2026** via API (Supabase + RunPod), com
verificação de leitura de volta nos dois lados — os templates JÁ têm a
config abaixo; falta recriar os pods e rodar os load tests.

### VibeCoder (A40 48GB, Qwen3.5-9B) — aplicado

```
--dtype bfloat16 --max-model-len 65536 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 --max-num-seqs 8 --served-model-name vibecoder-base
```

- Janela nativa do Qwen3.5-9B é **262144** (config.json conferido) —
  **sem YaRN/hf-overrides**, só a flag. Era 16384.
- KV fp8 do 9B = 64KB/token (32 camadas × 4 KV heads × 256 head_dim):
  sessão de 64k ≈ 4,2 GB; pool ~22 GB ≈ **~340k tokens ≈ ~5 sessões
  cheias** + leves.
- `kv_reserve_gb_per_user`: 1 → **1.5** (high = 4,5 GB ≈ uma sessão cheia;
  orçamento (48−20)/1.5 = 18 slots ponderados).
- `--max-num-seqs 8`: ponto de partida; calibrar no load test.

### Pro (L40S 48GB, Qwen3.6-27B-FP8) — aplicado

```
--max-model-len 65536 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 --max-num-seqs 16 --limit-mm-per-prompt {"image":0,"video":0} --served-model-name pro-base
```

- Era **40960**; janela nativa do 27B é 262k — sem YaRN, só a flag.
- **`--enable-prefix-caching` foi REMOVIDO**: estava no template em conflito
  com a política de pod compartilhado (o provisionamento seta
  `DISABLE_PREFIX_CACHING=true`, mas o `VLLM_EXTRA_ARGS` entra por último na
  linha de comando e o `--enable` vencia o `--no-enable` — canal lateral de
  timing entre tenants reaberto sem ninguém perceber).
- `--max-num-seqs 16` foi o mínimo pro pod SUBIR (histórico de OOM no boot);
  com sequências maiores, o load test decide se reduz.
- Orçamento: ~28 GB de pesos → pool de KV **~13 GB ≈ ~130k tokens fp8** ≈
  **~2 sessões cheias de 64k simultâneas** + usuários leves. É apertado de
  propósito (performance-first): quem impede a máquina de aceitar mais
  usuários pesados do que cabe são as classes de uso (high pesa 3.0 slots).
- `kv_reserve_gb_per_user`: 1 → **2** (high = 6 GB ≈ uma sessão cheia de
  64k no 27B, ~100KB/token fp8; orçamento (48−28)/2 = 10 slots ponderados).
- **128k no Pro foi avaliado e ADIADO**: uma sessão de 128k consumiria o
  pool inteiro da L40S. Exige A100 80GB (~3 sessões cheias) ou 2× L40S
  TP=2 (~4 sessões) — decisão de custo pendente.

## 3. Aplicação (o que resta — NÃO pular)

1. ~~Editar os templates~~ **FEITO (19/07/2026)**, com verify de leitura de
   volta no Supabase E no RunPod (precedente do updateTemplate silencioso).
2. Migrations 0031+0032 → push na main (ordem estrita da seção 1).
3. **Recriar os pods dos dois planos LOGO APÓS a migration 0031**: o
   backfill lê o env ATUAL do template (65536), mas os pods rodando ainda
   têm a janela antiga (VibeCoder 16384, Pro 40960) — até recriar, a coluna
   `machines.max_model_len` das máquinas antigas fica maior que a janela
   real e o clamp do gateway não protege (o vLLM volta a responder o 400
   cru, como hoje; não é pior que o estado atual, mas anula o benefício).
4. Conferir no log de boot do vLLM de cada pod novo:
   `max_model_len=65536` e `kv_cache_dtype=fp8`.
5. Conferir: `select name, max_model_len from machines;` → máquinas novas
   dos dois planos = 65536.

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
