-- Limite mensal de requisições por plano numa tabela (28/10/2026).
--
-- Até a 0079 o padrão do plano morava num `case` dentro de
-- `stack_request_quota` (Go 4.000, Pro 10.000), e mudar o número pedia
-- migration. Agora ele mora em `plan_request_quotas`, editada pela página Uso
-- do manager, e a função lê de lá. Os números novos entram junto:
-- Go 4.000 → 2.000, Pro 10.000 → 5.000.
--
-- Quem lê a tabela:
--   * `stack_request_quota` (gateway corta com ela; manager e TryStac mostram);
--   * o site do TryStac, que monta os textos de planos (FAQ, cards, tabela
--     comparativa) com estes números — por isso a leitura é pública. Não há
--     nada sensível aqui: é o mesmo número que a página de preços anuncia.
--
-- O resto da 0079 não muda: request ponderada, ciclo no aniversário de
-- purchase_date, `stacks.request_quota_override` (0077) por cima do padrão.
-- VibeCoder continua seguindo o Go, como no `case` antigo.
--
-- Plano sem linha na tabela = sem teto (Max, Enterprise), igual ao `else null`
-- de antes. A tabela só aceita Go e Pro para um typo no manager não criar
-- teto para um plano que vende "ilimitado".
--
-- A tabela de retorno da função é a mesma da 0079: `create or replace` basta.
--
-- Ordem de deploy: antes do manager novo (a página Uso lê a tabela) e antes do
-- TryStac novo (sem a tabela o site cai nos números de fallback do código,
-- que são estes mesmos). O gateway não muda.
--
-- Idempotente: pode rodar mais de uma vez. O seed não sobrescreve um limite
-- que já tenha sido alterado pelo manager.

begin;

create table if not exists public.plan_request_quotas (
  plan text primary key check (plan in ('Go', 'Pro')),
  quota_limit integer not null check (quota_limit > 0),
  updated_at timestamptz not null default now(),
  -- e-mail do admin do manager que fez a última troca; null no seed
  updated_by text
);

insert into public.plan_request_quotas (plan, quota_limit)
values ('Go', 2000), ('Pro', 5000)
on conflict (plan) do nothing;

alter table public.plan_request_quotas enable row level security;

drop policy if exists plan_request_quotas_public_read on public.plan_request_quotas;
create policy plan_request_quotas_public_read
  on public.plan_request_quotas
  for select
  to anon, authenticated
  using (true);

-- Escrita só pela service_role (manager), que ignora RLS.
revoke all on table public.plan_request_quotas from anon, authenticated;
grant select on table public.plan_request_quotas to anon, authenticated;
grant all on table public.plan_request_quotas to service_role;

create or replace function public.stack_request_quota(
  p_stack_id uuid,
  p_at timestamptz default now()
)
returns table (
  plan text,
  quota_limit integer,
  used bigint,
  cycle_start timestamptz,
  cycle_end timestamptz,
  quota_default integer,
  used_weighted bigint
)
language sql
stable
security invoker
set search_path = ''
as $$
  select
    s.plan,
    case
      when coalesce(s.category, 'llm') <> 'llm' then null
      else coalesce(s.request_quota_override, p.quota_default)
    end,
    u.used,
    c.cycle_start,
    c.cycle_end,
    p.quota_default,
    u.used_weighted
  from public.stacks s
  cross join lateral (
    select case
      when coalesce(s.category, 'llm') <> 'llm' then null
      else (
        select q.quota_limit
        from public.plan_request_quotas q
        where q.plan = case when s.plan = 'VibeCoder' then 'Go' else s.plan end
      )
    end as quota_default
  ) p
  cross join lateral public.stack_quota_cycle(s.purchase_date, p_at) c
  cross join lateral (
    select
      count(*) as used,
      coalesce(sum(public.request_weight(g.tokens_in)), 0)::bigint as used_weighted
    from public.gateway_requests g
    -- left join: api_key_id vira nulo quando a chave é apagada (0066), e o
    -- uso dela continua sendo da stack
    left join public.api_keys k on k.id = g.api_key_id
    where g.stack_id = s.id
      and g.created_at >= c.cycle_start
      and g.created_at < c.cycle_end
      and k.purpose is distinct from 'playground'
  ) u
  where s.id = p_stack_id
$$;

revoke all on function public.stack_request_quota(uuid, timestamptz) from public, anon;
grant execute on function public.stack_request_quota(uuid, timestamptz)
  to authenticated, service_role;

commit;
