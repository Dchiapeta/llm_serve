-- Cabeças voltam a ser cabeças + teto de heavy por máquina.
--
-- A 0032 fez a ocupação da máquina virar SOMA DE PESOS (low=1, medium=1.5,
-- high=3). O efeito colateral é que a capacidade COMERCIAL encolhe com o
-- perfil dos clientes: uma máquina de 18 slots com 6 stacks 'high' ficava
-- CHEIA com 6 contas. Isso inverte a promessa do plano — 18 slots têm que
-- comportar 18 contas, sempre, seja qual for a mistura.
--
-- Desenho novo: duas restrições INDEPENDENTES, em vez de uma ponderada.
--
--   1. cabeças  machine_stack_load  (contagem) <  machine_stack_slots
--   2. mistura  machine_high_count             <  machine_high_cap
--
-- (2) é SUB-TETO, não reserva: os lugares de heavy não ficam parados quando
-- não há heavy. Com cap 7 numa máquina de 18 cabeças, é perfeitamente válido
-- ter 1 heavy + 17 low/medium. O que não pode existir é o 8º heavy.
--
-- Só 'high' tem teto, por decisão de produto: medium/low não degradam a
-- experiência dos co-tenants o suficiente pra justificar mais uma restrição.
--
-- De onde saem os números do max_high (ver UPDATEs no fim): o gargalo destes
-- modelos NÃO é VRAM. Qwen3.5/3.6 são híbridos (full_attention_interval=4, só
-- 1 em 4 camadas tem KV que cresce), então o KV por token é baixíssimo — 16
-- KiB no 9B, 32 KiB no 27B — e sobra mais de 1 M de tokens de KV nas máquinas
-- de 48 GB. O que satura é GPU-segundo de PREFILL: um turno de usuário heavy
-- (~85k tokens de contexto) custa dezenas de segundos de GPU exclusiva. Logo
-- o teto sai de "quantos turnos heavy cabem na fila dentro do TTFT alvo", não
-- de GB de VRAM.
--
-- IMPORTANTE: os valores abaixo pressupõem prefix caching LIGADO (o delta por
-- turno cai de ~85k tokens pra ~4k). Com o caching desligado — situação de
-- hoje, porque o vLLM desabilita prefix caching sozinho em modelos híbridos —
-- um único turno heavy já ocupa a GPU por dezenas de segundos e nenhum teto
-- salva a máquina. O teto é a política CERTA, mas quem entrega a performance
-- é o caching; ver docker/entrypoint.sh.

-- 1. Ocupação = CONTAGEM de cabeças (reverte a ponderação da 0032).
--    Assinatura preservada (numeric) pra não quebrar os chamadores existentes
--    no gateway (supa.machine_stack_load) nem o painel.
create or replace function machine_stack_load(p_machine_id uuid)
returns numeric
language sql
security definer
as $$
  select count(*)::numeric
  from stacks s
  where s.machine_id = p_machine_id
$$;

-- 2. Quantas stacks 'high' a máquina hospeda (a restrição de mistura).
create or replace function machine_high_count(p_machine_id uuid)
returns integer
language sql
security definer
as $$
  select count(*)::integer
  from stacks s
  where s.machine_id = p_machine_id
    and s.usage_class = 'high'
$$;

-- 3. Teto de heavy da máquina, lido de templates.usage_class_config->>'max_high'.
--    NULL = SEM TETO, e isso é fail-open deliberado: máquina sem template, ou
--    template sem a chave configurada, se comporta exatamente como antes desta
--    migration. Um teto ausente nunca pode bloquear alocação — o pior caso de
--    fail-open é uma máquina desbalanceada; o de fail-closed seria cliente sem
--    máquina nenhuma.
create or replace function machine_high_cap(p_machine_id uuid)
returns integer
language sql
stable
security definer
as $$
  select (t.usage_class_config->>'max_high')::integer
  from machines m
  left join templates t on t.id = m.template_id
  where m.id = p_machine_id
$$;

-- usage_class_weight (0032) fica DEFINIDA de propósito: não é mais usada em
-- admissão, mas continua sendo o espelho SQL de lib/capacity.ts:stackWeight e
-- usage_class.py:class_weight, que o painel usa para exibir intensidade de uso.
-- Removê-la quebraria o display sem ganho nenhum.

-- Tetos por template. Derivados em GPU-segundos de prefill por turno heavy,
-- com fila alvo <= 15s e prefix caching ligado:
--   VibeCoder A40 (9B bf16, ~38 TFLOPS efetivos)     -> 1.9 s/turno -> 7
--   Pro L40S (27B-FP8 nativo, ~183 TFLOPS efetivos)  -> 1.2 s/turno -> 7 (limitado por KV)
--   Pro 2xA40 (27B-FP8 via Marlin, ~64 TFLOPS efet.) -> 3.4 s/turno -> 4
-- O 2xA40 é o mais apertado porque o A40 não tem FP8 nativo (CC 8.6 < 8.9) e
-- cai em dequant Marlin, e porque TP=2 sem NVLink perde ~15% em all-reduce.
--
-- jsonb concat (||) em vez de atribuição: preserva qualquer outra chave que já
-- exista em usage_class_config (weights, daily_*, req_pct_*).
update templates
set usage_class_config = coalesce(usage_class_config, '{}'::jsonb)
                         || jsonb_build_object('max_high', 7)
where name = 'VibeCoder_A40_Qwen3.5';

update templates
set usage_class_config = coalesce(usage_class_config, '{}'::jsonb)
                         || jsonb_build_object('max_high', 7)
where name = 'Pro_L40S_Qwen3.6-27B';

update templates
set usage_class_config = coalesce(usage_class_config, '{}'::jsonb)
                         || jsonb_build_object('max_high', 4)
where name = 'Pro_2xA40_Qwen3.6-27B_128K';
