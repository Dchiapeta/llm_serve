-- Classificação de usuários por padrão de consumo (Baixo/Médio/Alto) +
-- ocupação PONDERADA de máquina.
--
-- Motivação: com a janela do VibeCoder subindo pra 64k (Claude Code), o que
-- protege a experiência dos co-tenants não é o número de stacks por máquina,
-- e sim a MISTURA de perfis — um usuário de contexto longo consome um
-- múltiplo do KV de um usuário de chat. Cada stack ganha uma classe de uso
-- (derivada do consumo real em usage_metrics, loop no gateway) e a ocupação
-- da máquina passa a ser a soma dos PESOS das classes, não a contagem.
--
-- Pesos default: low=1.0 (preserva exatamente a matemática atual — máquina
-- só de lows tem a mesma capacidade de antes), medium=1.5, high=3.0.
-- Override por template em templates.usage_class_config (jsonb), ex.:
--   {"weights": {"low": 1, "medium": 2, "high": 4},
--    "daily_medium": 300000, "daily_high": 1500000,
--    "req_pct_medium": 0.15, "req_pct_high": 0.40}
--
-- A classe é sempre RELATIVA ao plano da stack (limiares de tokens/request
-- em fração da janela do plano; limiares diários por plano) e só influencia
-- alocações FUTURAS (realocação automática, migrateStack, stack nova) —
-- nunca dispara migração forçada.

alter table stacks add column if not exists usage_class text not null default 'low'
  check (usage_class in ('low', 'medium', 'high'));
alter table stacks add column if not exists usage_class_updated_at timestamptz;

alter table templates add column if not exists usage_class_config jsonb;

-- Peso de uma stack pela classe, com override opcional do template.
-- Espelho TS: stackWeight em lib/capacity.ts; espelho Python:
-- usage_class.py (gateway) — manter os três em sincronia.
create or replace function usage_class_weight(p_class text, p_config jsonb)
returns numeric
language sql
immutable
as $$
  select coalesce(
    (p_config->'weights'->>coalesce(p_class, 'low'))::numeric,
    case coalesce(p_class, 'low')
      when 'high' then 3.0
      when 'medium' then 1.5
      else 1.0
    end
  )
$$;

-- Ocupação ponderada da máquina: soma dos pesos das stacks hospedadas.
-- Par do machine_stack_slots (0018), que segue sendo o ORÇAMENTO; a vaga
-- efetiva é machine_stack_slots − machine_stack_load ≥ peso do entrante.
create or replace function machine_stack_load(p_machine_id uuid)
returns numeric
language sql
security definer
as $$
  select coalesce(sum(usage_class_weight(s.usage_class, t.usage_class_config)), 0)
  from stacks s
  left join machines m on m.id = s.machine_id
  left join templates t on t.id = m.template_id
  where s.machine_id = p_machine_id
$$;

-- Agregados por stack pro loop de classificação do gateway (janela móvel):
-- consumo total, requests e dias ativos, mais o contexto necessário pra
-- classificar (plano, janela do modelo da máquina atual, config do template
-- e estado atual da classe, pra histerese/cooldown).
create or replace function stack_usage_stats(p_days integer default 14)
returns table (
  stack_id uuid,
  plan text,
  usage_class text,
  usage_class_updated_at timestamptz,
  max_model_len integer,
  usage_class_config jsonb,
  total_tokens bigint,
  total_requests bigint,
  active_days integer
)
language sql
stable
security definer
as $$
  select
    s.id,
    s.plan,
    s.usage_class,
    s.usage_class_updated_at,
    m.max_model_len,
    t.usage_class_config,
    sum(u.tokens_in + u.tokens_out)::bigint,
    sum(u.requests)::bigint,
    count(distinct (u.window_start at time zone 'utc')::date)::integer
  from usage_metrics u
  join api_keys k on k.id = u.api_key_id
  join stacks s on s.id = k.stack_id
  left join machines m on m.id = s.machine_id
  left join templates t on t.id = m.template_id
  where u.window_start >= now() - make_interval(days => p_days)
  group by s.id, s.plan, s.usage_class, s.usage_class_updated_at,
           m.max_model_len, t.usage_class_config
$$;
