# Claude Code no plano Pro (janela 128k)

> **O Go saiu deste doc.** Ferramenta de código passou a ser do Pro para cima: o
> gateway responde `403` em `/v1/messages`, `/v1/responses` e em
> `chat/completions` com User-Agent de CLI quando o plano é Go
> (`docker/gateway/cli_policy.py`). A config do template Go abaixo fica como
> **registro histórico** — foi dimensionada para o Claude Code, e é justamente o
> que a torna superdimensionada para o perfil de requests de API que sobrou.

Por que existe: o Claude Code CLI manda ~26k tokens só de system prompt +
tool schemas antes de qualquer trabalho — numa janela de 16k ele nem abre
sessão (era o 400 cru do vLLM: "maximum context length is 16384 tokens...")
e em 32k compacta o tempo todo. A janela subiu 16k → 32k → 64k → **131072
(128k)**. Este doc cobre a config do template, o gate de load test e o setup
do usuário.

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

Regras comuns:
- **Prefix caching é LIGADO, com isolamento por tenant.** Este item já disse o
  contrário ("NÃO ligar") e ficou desatualizado: as migrations 0040 (Go) e 0042
  (Pro) o habilitaram via `PREFIX_CACHE_ISOLATION=cache_salt`, que é o que
  resolve o canal lateral de timing entre co-tenants que antes o bloqueava.
  Em `docker/entrypoint.sh` o `PREFIX_CACHE_ISOLATION=cache_salt` vence o
  `DISABLE_PREFIX_CACHING=true` que `podInputFromTemplate` injeta para
  `SHARED_POD_PLANS` — o `if` vem antes. E a flag explícita é **obrigatória**
  nesses modelos: em modelo híbrido o vLLM desliga o prefix caching sozinho,
  em silêncio, se ninguém passar `--enable-prefix-caching`.
- `kv_reserve_gb_per_user` = a unidade de um usuário **low** (peso 1.0);
  um high custa 3× isso na ocupação.

**STATUS: aplicado em 19/07/2026** via API (Supabase + RunPod), com
verificação de leitura de volta nos dois lados — os templates JÁ têm a
config abaixo; falta recriar os pods e rodar os load tests.

### Go (A40 48GB, Qwen3.5-9B) — histórico, plano sem CLI desde 11/08/2026

Mantido aqui porque explica de onde vem a config que o Go tem HOJE, e porque é
o ponto de partida de qualquer recalibração para o perfil novo.

**Quem serve o plano hoje é o `Go_A40_Qwen3.5_64K-TEST`** (Supabase
`6ef2312b-a9c7-4566-a763-dd24709416ab`, RunPod `frepvqwivj`): `--max-model-len
65536`, `--max-num-seqs 16`, `kv_reserve 1.0`, `max_users 25`. Ele deixou de ser
candidato e passou a ser produção — o `Go_A40_Qwen3.5` desta seção está
`is_enabled: false`, e `getDefaultTemplateForPlan` filtra por `is_enabled=true,
is_test=false`. O sufixo `-TEST` no nome é resíduo, não status.

O candidato atual a substituí-lo é o **`GO-Teste`** (Supabase
`cd74b56d-6da0-4a5f-98bb-31d90421167f`, RunPod `yhzwlzckt3`, `is_test: true`),
que porta ao Go as duas otimizações medidas no Pro: `RedHatAI/Qwen3.5-9B-quantized.w4a16`
(4-bit compressed-tensors — 15,87 → 8,00 GB lidos por step de decode) e, numa
fase seguinte, `--speculative-config {"method":"mtp","num_speculative_tokens":2}`.
Promover depende de load test — o perfil a medir é concorrência de requests de
API, não contexto grande de sessão agêntica.

```
--dtype bfloat16 --max-model-len 131072 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 --max-num-seqs 8 --served-model-name go-base
```

- O alias servido é **`go-base`**. Este item já afirmou o contrário
  (`vibecoder-base`, "quem copiar o nome errado leva 404") e ficou
  desatualizado: em 31/08/2026 o `--served-model-name go-base` foi gravado nos
  dois templates Go, no Supabase e no RunPod. Confirmado no banco e nos pods
  recentes (`llm-stack-401`, `402`, `413`, todos com
  `machines.served_model_name = go-base`). O que continua valendo é o mecanismo:
  o vLLM lê a flag só no boot, então um pod já de pé segue servindo o alias
  antigo até ser **recriado** — start/stop não basta.
- Janela nativa do Qwen3.5-9B é **262144** (config.json conferido) —
  **sem YaRN/hf-overrides**, só a flag. Era 16384 → 65536 → **131072 (128k,
  aplicado 23/07/2026)**; admissão mantida (kv_reserve 1.5, max_users 18).
- KV fp8 do 9B = **16 KiB/token**. ⚠️ Este número já esteve errado aqui como
  "64 KB/token", e o erro importa: ele fazia uma sessão cheia de 128k parecer
  custar 8,4 GB (≈2,6 sessões no pool) quando custa **2 GB** (≈11 sessões). O
  9B é **híbrido** — `full_attention_interval=4`, só 8 das 32 camadas têm KV que
  cresce — e é de lá que sai o 4× de diferença (medição na migration 0037 e em
  `modelos-hibridos-prefix-caching-off`). Consequência prática: **o KV nunca foi
  o recurso escasso do Go**; o pool de ~22 GB serve ~1,4 M tokens e quem corta a
  concorrência é `--max-num-seqs`.
- Subir o teto de janela não consome mais VRAM — o pool é fixado pelo
  `gpu-memory-utilization`, e `--max-model-len` é teto por sequência, não
  reserva. **Baixá-lo, portanto, também não devolve VRAM**: o que ele encolhe é
  o pior caso por sessão, e é isso que permite subir `--max-num-seqs` sem
  preempção.
- `kv_reserve_gb_per_user`: 1 → **1.5** (high = 4,5 GB; orçamento
  (48−20)/1.5 = 18 slots). Note que este campo trata cada cliente
  **contratado** como se tivesse KV reservado permanentemente — premissa
  razoável para sessão de CLI, falsa para requests de API, onde o KV é liberado
  no fim de cada request.
- `--max-num-seqs 8`: ponto de partida; calibrar no load test.

### Pro (2× A40 48GB, TP=2, Qwen3.6-27B-FP8) — aplicado

Template `Pro_2xA40_Qwen3.6-27B_128K` (Supabase `a46b1566-…`), hoje o **único**
template do plano Pro — o template L40S saiu de produção. `gpu_types=["NVIDIA
A40"]`, `gpu_count=2` (o entrypoint deriva `--tensor-parallel-size 2` do
`GPU_COUNT`), imagem `dchiapeta/vllm-agent:latest`.

```
--max-model-len 131072 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 --max-num-seqs 16 --disable-custom-all-reduce --enable-prompt-tokens-details --served-model-name pro-base
```

- **Janela igual à do Go: 131072.** É o que permite uma config única de
  cliente nos dois planos (seção 5). Nativa do 27B é 262k — sem YaRN, só a flag.
- Orçamento: 96 GB totais, pesos ~28 GB fatiados 14+14 no TP → pool de KV
  **~54 GB ≈ ~540k tokens fp8** ≈ **~4,2 sessões cheias de 128k**. Admissão:
  `kv_reserve_gb_per_user=4.3`, `max_users=15` ≈ ~5 pesados / ~15 leves
  (`usage_class_config.max_high=4`).
- Input útil real ≈ **123k** (o piso global `MIN_MAX_TOKENS=8000` do gateway
  reserva a saída).
- **`NCCL_P2P_DISABLE=1` + `NCCL_IB_DISABLE=1` + `--disable-custom-all-reduce`
  são obrigatórios**, não otimização: 2× A40 em PCIe sem NVLink deadlocka na
  primeira coletiva do NCCL e o pod nunca fica ready. Vale para qualquer
  template com `gpu_count > 1`.
- Isolamento de prefix cache por tenant via `PREFIX_CACHE_ISOLATION=cache_salt`
  (migration 0042) — pod compartilhado, o `--enable-prefix-caching` cru
  reabriria canal lateral de timing entre co-tenants.
- **Caveat que resta é latência, não capacidade:** a A40 não tem FP8 nativo
  (CC 8.6 < 8.9) e cai em dequant Marlin, e TP=2 sem NVLink perde ~15% no
  all-reduce por camada. Por isso a 0037 dá ao Pro 2xA40 só **4 cabeças** — é o
  template mais apertado dos três. O load test tem que medir **TTFT (prefill)**
  a 128k; é ele que decide se essa opção barata serve ao princípio
  performance-first, e continua **pendente**.

## 3. Aplicação

1. ~~Editar os templates~~ **FEITO**: os dois planos estão em `--max-model-len
   131072` no Supabase e no RunPod, com verify de leitura de volta nos dois
   lados (precedente do `updateTemplate` que engole erro silenciosamente).
2. ~~Migrations 0031+0032~~ **FEITO**.
3. **Pods antigos precisam ser recriados**: `--max-model-len` só muda
   reiniciando o vLLM. Enquanto um pod roda com a janela antiga, a coluna
   `machines.max_model_len` (parseada do template no insert) fica **maior** que
   a janela real do processo, e o clamp do gateway não protege — o vLLM volta a
   responder o 400 cru. Não é pior que não ter clamp, mas anula o benefício.
4. Conferir no log de boot do vLLM de cada pod novo: `max_model_len=131072`,
   `kv_cache_dtype=fp8`, capacidade real do pool de KV e — no Pro —
   `tensor_parallel_size=2` mais a passagem pelo NCCL (o deadlock de PCIe sem
   NVLink congela exatamente em `parallel_state.py`/`pynccl.py`).
5. Conferir: `select name, max_model_len from machines;` → **131072**.

## 4. Load test (gate — a mudança só vale se passar)

Payloads estilo Claude Code = contexto sintético grande + streaming.

O teto de input sintético subiu: com a margem por procedência
(`CONTEXT_EXACT_SAFETY_FACTOR = 1.02`, ver seção 5), input perto da janela
**deixou de ser rejeição esperada** — o gateway escala pro tokenizer real
acima de ~58% da janela e reserva apenas 2% + 200 tokens. Em 131072 dá pra
testar até `--context-tokens 110000`, e um 400 de contexto aí é falha do teste,
não comportamento previsto.

```bash
# Pro (2× A40 TP=2, 27B: pool ~540k tokens ≈ ~4,2 sessões cheias)
python3 scripts/loadtest.py \
  --base-url https://api.trystac.com \
  --api-key <chave Pro> --model <alias pro> \
  --levels 2,4,6 --context-tokens 110000 --max-tokens 16000
```

No Pro o número que decide não é vazão, é **TTFT**: o gargalo é o prefill de
128k (dequant Marlin + all-reduce por camada em PCIe). Capacidade de KV lá
sobra; latência é a incógnita.

E uma rodada de regressão com as tarefas normais (sem `--context-tokens`), igual
à validação do template v2 (70/70).

Critérios de aprovação:
- zero 400 de contexto e zero OOM/crash do vLLM;
- TTFT aceitável no pico (referência: Pro ~10s em carga pesada);
- regressão das tarefas normais sem degradação relevante.

Se degradar: reduzir `--max-num-seqs`. Reduzir a janela é o último recurso —
`AUTO_COMPACT_WINDOW` sai da janela real por máquina (`lib/context-window.ts`),
então o painel acompanha sozinho, mas os docs e o onboarding perdem o número
único.

Cuidado ao testar direto no pod (bypass do gateway): o idle-reaper não vê
atividade e pode pausar a máquina no meio do teste — preferir sempre o
gateway.

## 5. Setup do usuário (onboarding)

O painel gera esses dois blocos na aba **Ferramentas** da máquina, já com a URL,
o alias do modelo e a janela preenchidos (`components/machines/machine-about.tsx`,
conta em `lib/context-window.ts`). A aba só aparece em máquina de plano com CLI —
numa máquina Go ela fica oculta, pela mesma constante que o gateway aplica
(`CLI_BLOCKED_PLANS`).

Permanente — recomendado, é o que não se esquece:

```json
// ~/.claude/settings.json   (o da SUA CONTA, não o do projeto: esse vai pro git)
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.trystac.com",
    "ANTHROPIC_AUTH_TOKEN": "<sua chave do plano>",
    "ANTHROPIC_MODEL": "<alias do modelo do plano>",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "104000"
  }
}
```

Valores em `env` são **strings**, e não há interpolação — `"$ANTHROPIC_MODEL"`
não funciona em JSON, o alias tem que ser repetido literalmente em cada chave
`ANTHROPIC_DEFAULT_*`.

Só para a sessão do terminal atual:

```bash
export ANTHROPIC_BASE_URL=https://api.trystac.com
export ANTHROPIC_AUTH_TOKEN=<sua chave do plano>
export ANTHROPIC_MODEL=<alias do modelo do plano>
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=104000
```

**Por que 104000.** A variável não é o gatilho: ela declara a **capacidade** que
o Claude Code passa a assumir, e ele compacta numa fração *interna* dela — entre
~80% e ~92%, que varia por versão e não é configurável. O valor sai de **dois**
tetos:

1. **Espaço** — `usable_input_tokens`: 131072 − 8000 de saída − 200 de template,
   com 2% de margem = **120462**. Acima disso o gateway rejeita.
2. **Transbordo** — `CONTEXT_OVERSHOOT_MARGIN` (16000): o cliente decide olhando
   o contexto do turno *anterior*, e entre a decisão e a request seguinte cabe um
   `tool_result` inteiro. Um `Read` de arquivo de 60 KB são ~18k tokens.

| Declarado | Compacta em | Sobra até o teto de admissão (128054) |
|---|---|---|
| 120000 (só o teto 1) | 96k–110k | **17654** — menos que um arquivo grande |
| **104000** | **83k–96k** | **32374** — absorve um turno grande inteiro |

Até 11/08/2026 o valor era 120000, com o argumento de que declarar menos deixaria
~15% de contexto na mesa. O argumento não estava errado, estava **incompleto**:
otimizava aproveitamento de janela e ignorava o orçamento de transbordo. Foi o que
produziu o incidente de **128465 tokens** — estouro de 411 tokens (0,3%) sobre o
teto, com a sessão morta, porque `/compact` reenvia a transcrição e é justamente o
pedido que estoura. Contexto na mesa custa 15% de uma sessão; estourar custa a
sessão inteira.

Fórmula única em `context_budget.auto_compact_window()`, com o valor fixado pelos
testes `test_janela_recomendada_ao_cliente`,
`test_janela_recomendada_reserva_o_transbordo_de_um_turno` e
`test_teto_de_admissao_e_a_fronteira_exata_do_400`. Se a janela de um plano mudar,
é de lá que o número novo sai — e `lib/context-window.ts` tem que acompanhar.

Notas para o usuário:
- Não existe config oficial de "tamanho de janela" no Claude Code para
  endpoints customizados: ele assume 200k e ignora `/v1/models`
  (anthropics/claude-code#46416, #68522). `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
  é o único mecanismo suportado para compactar antes do limite real.
- Quando o contexto estoura mesmo assim, o gateway responde com erro claro
  instruindo `/clear` — e **não** `/compact`: `/compact` reenvia a transcrição
  inteira, então é justamente o pedido que dispara o erro. Mandar usar
  `/compact` ali era um beco sem saída (a sessão travava sem saída nenhuma).
- Codex CLI: o equivalente é `model_auto_compact_token_limit` no
  `~/.codex/config.toml` (mesmo 104000). NÃO usar `model_context_window`
  (bug openai/codex#16068 quebra a compaction).
- Boas práticas: CLAUDE.md do projeto enxuto, usar subagents para pesquisa
  longa (contexto zerado), `/compact` manual em sessões longas — bem antes do
  limite, não em cima dele.

### Histórico: os bugs que faziam isso não funcionar (corrigidos)

Até 11/08/2026, configurar a janela acima não bastava. Foram **três** rodadas —
as duas primeiras (31/07) estavam certas mas não fechavam o problema, porque a
causa que realmente derrubava a compactação só apareceu na terceira:

1. **O auto-compact nunca disparava.** O gateway emitia
   `message_start.usage.input_tokens = 0` em toda resposta streaming
   (`anthropic_compat.py`), porque o vLLM só manda `usage` no chunk final. O
   Claude Code rastreia o contexto por esse campo, então o contador dele ficava
   parado em zero — qualquer que fosse a janela configurada. Hoje o
   `message_start` leva a estimativa do orçamento de contexto (que perto do
   teto é a contagem exata do tokenizer) e o `message_delta` corrige com o
   número real do vLLM.
2. **`/compact` falhava com 400.** `reserved_tokens_for` aplicava a margem de
   1.2 (de estimativa heurística) mesmo sobre a contagem EXATA do tokenizer:
   122658 tokens reais numa janela de 131072 reservavam 147390 e eram
   rejeitados, quando cabiam com ~8k de sobra. Hoje a margem é por procedência
   (`EstimateKind`): 1.02 para contagem exata, 1.1 quando a contagem falhou,
   1.2 para heurística. Isso também devolveu ~18% de input útil por sessão.

3. **A compactação morria em silêncio, e o gateway chamava isso de sucesso.**
   Esta era a causa de verdade. O read timeout do streaming (60s, herdado do
   `proxy_client`) cobria também o **TTFT** — o tempo até o primeiro chunk, que
   é o prefill. Como a compactação reenvia a transcrição inteira, ela é a
   requisição de maior prefill que existe: no Pro, ~85k tokens já custam ~72s
   só de prefill. Estourado o prazo, o `ReadTimeout` caía num
   `except (Exception,): pass` mudo e o gerador seguia direto para
   `message_delta` com `stop_reason: "end_turn"` e `message_stop`, sob HTTP 200
   — **um turno vazio que afirmava sucesso**.

   O efeito: o auto-compact disparava, a request morria vazia, o Claude Code
   recebia um "resumo" em branco e a conversa continuava crescendo até estourar
   a janela. O `/compact` manual fazia o mesmo — daí o sintoma "trava e não
   retorna nada". E nada disso aparecia em lugar nenhum: o `pass` era mudo, o
   `anthropic_compat.py` não tinha logger, e o 400 de contexto em `/v1/messages`
   nunca chamava `log_gateway_request` (não existia em `gateway_requests`).

   Hoje: watchdog de duas fases (`MESSAGES_STREAM_TTFT_TIMEOUT_S` para o
   prefill, `MESSAGES_STREAM_IDLE_TIMEOUT_S` para o silêncio entre chunks),
   `event: ping` a cada 15s enquanto se espera, e stream que morre sem gerar
   nada vira `event: error` em vez de mensagem vazia. Junto: `upstream.aclose()`
   no `finally` (cada stream abandonado queimava um slot do pool de conexões) e
   os quatro campos de `usage` em todos os pontos de emissão — faltavam
   `cache_*` no `message_delta` e no não-streaming, e num cliente JS
   `undefined` propaga `NaN`, que nunca ultrapassa limiar nenhum.

Ver `docker/gateway/context_budget.py`, `docker/gateway/anthropic_compat.py` e
os testes de regressão em `test_context_budget.py` /
`test_anthropic_compat.py` / `test_resolve_est_tokens.py`.

**Assinatura do bug 3 no banco**, se ele voltar: `gateway_requests` com
`path='messages'`, `stream=true`, `status_code=200`, `tokens_out` nulo e
`duration_ms ≈ 60000`.

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
  sozinhos da janela nova (fração de 128k); os diários (Go 300k/1.5M,
  Pro 600k/3M por dia) são os candidatos à calibração.
