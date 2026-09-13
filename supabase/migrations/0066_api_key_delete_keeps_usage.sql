-- Excluir uma chave deixa de apagar o histórico de uso dela.
--
-- BUG: usage_metrics.api_key_id nasceu (0001) com `on delete cascade`. O
-- painel do cliente (TryStac, action deleteApiKey) fazia DELETE de verdade em
-- api_keys, e o Postgres levava junto toda linha de usage_metrics da chave —
-- 1MM de tokens consumidos sumiam do painel no instante em que o usuário
-- apagava a chave. A denormalização de stack_id (0036) não protegia nada:
-- ela cobre chave ÓRFÃ (linha sem stack), não linha DELETADA.
--
-- Efeitos colaterais que o cascade também causava:
--   * account_token_usage_today (0025/0044) somava via `join api_keys`, então
--     apagar a chave zerava o consumo do dia da conta — bastava apagar e
--     recriar a chave pra furar a cota diária do plano (check_token_quota).
--   * stack_usage_stats (0032, recriada na 0023 do TryStac) também resolvia a
--     stack via api_keys, e perdia o histórico da classificação de uso.
--
-- O que muda aqui:
--   1. A FK passa a `on delete set null` — mesmo contrato de gateway_requests
--      (0038), image_generations (0059) e stack_clients (0051). A linha de uso
--      sobrevive à chave; `stack_id` (0036) continua identificando a stack, e
--      é por ele que o painel lê (view stack_token_totals e a RLS
--      usage_metrics_select_own_stack, 0017 do TryStac). O unique
--      (api_key_id, machine_id, window_start) aceita nulos sem conflito.
--   2. As duas RPCs de agregação passam a resolver conta/stack por
--      usage_metrics.stack_id, com api_keys em LEFT JOIN só para o filtro de
--      `purpose`. Linha sem chave (hard delete por qualquer caminho futuro)
--      continua contando para a cota e para a classificação.
--   3. `status` de api_keys ganha o valor 'deleted': o painel do cliente passa
--      a fazer soft delete (TryStac, migration 0041 de lá, que também REVOGA
--      o privilégio de DELETE de `authenticated`). Gateway (find_active_key),
--      agent (syncMachineKeys) e contagem de slot já filtram `status =
--      'active'`, então uma chave 'deleted' para de funcionar e de contar
--      exatamente como uma 'revoked' — a diferença é só o que o painel exibe.
--      O CHECK entra como NOT VALID de propósito: a coluna nunca teve
--      constraint e este repo não enxerga o que o outro histórico de
--      migrations pode ter gravado (ver SHARED_SCHEMA.md); validar linhas
--      antigas aqui poderia falhar o deploy por um valor fantasma.
--
-- Nenhum dado já apagado pelo cascade volta com esta migration — só backup /
-- PITR do projeto recupera, ou a reconstrução aproximada por
-- gateway_requests (que tem stack_id, tokens_in e tokens_out por request e
-- nunca foi apagada: a FK dela já era set null).
--
-- Idempotente: pode rodar mais de uma vez.

begin;

-- 1. FK: cascade -> set null. O nome da constraint não é fixado na 0001 (veio
-- do default do Postgres), e o banco real pode divergir do histórico daqui,
-- então localiza por catálogo em vez de assumir `usage_metrics_api_key_id_fkey`.
do $$
declare
  c record;
begin
  for c in
    select con.conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    join pg_class frel on frel.oid = con.confrelid
    where nsp.nspname = 'public'
      and rel.relname = 'usage_metrics'
      and frel.relname = 'api_keys'
      and con.contype = 'f'
      and con.conkey = array[
        (select attnum from pg_attribute
         where attrelid = rel.oid and attname = 'api_key_id')
      ]
  loop
    execute format('alter table public.usage_metrics drop constraint %I', c.conname);
  end loop;
end $$;

alter table public.usage_metrics
  add constraint usage_metrics_api_key_id_fkey
  foreign key (api_key_id) references public.api_keys(id) on delete set null;

-- 2a. Cota diária: resolve a conta pela stack gravada na própria linha
-- (0036), caindo na conta da chave só quando a linha é anterior à
-- denormalização e ainda tem chave. `purpose` nulo (linha sem chave) é
-- tratado como 'customer': uso sem dono identificável continua sendo custo
-- da conta, e a única chave isenta de cota (playground, 0044) nunca é
-- apagada pelo cliente.
create or replace function public.account_token_usage_today(p_account_id uuid)
returns bigint
language sql
security definer
stable
set search_path = ''
as $$
  select coalesce(sum(um.tokens_in + um.tokens_out), 0)::bigint
  from public.usage_metrics um
  left join public.stacks s on s.id = um.stack_id
  left join public.api_keys ak on ak.id = um.api_key_id
  where coalesce(s.account_id, ak.account_id) = p_account_id
    and coalesce(ak.purpose, 'customer') = 'customer'
    and um.window_start >= date_trunc('day', now())
$$;

-- 2b. Classificação de uso: mesma troca de caminho. Assinatura, `security
-- definer`, `search_path = ''` e grants seguem os da 0023 do TryStac (última
-- definição aplicada no banco) — `create or replace` preserva os grants.
create or replace function public.stack_usage_stats(p_days integer default 14)
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
set search_path = ''
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
  from public.usage_metrics u
  join public.stacks s on s.id = u.stack_id
  left join public.machines m on m.id = s.machine_id
  left join public.templates t on t.id = m.template_id
  where u.window_start >= now() - make_interval(days => p_days)
  group by s.id, s.plan, s.usage_class, s.usage_class_updated_at,
           m.max_model_len, t.usage_class_config
$$;

-- 3. Terceiro status. NOT VALID: só linhas novas/alteradas são checadas.
do $$
begin
  if not exists (
    select 1
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    where nsp.nspname = 'public'
      and rel.relname = 'api_keys'
      and con.conname = 'api_keys_status_check'
  ) then
    alter table public.api_keys
      add constraint api_keys_status_check
      check (status in ('active', 'revoked', 'deleted')) not valid;
  end if;
end $$;

commit;
