-- Limite de requisições editável por stack (02/10/2026).
--
-- A 0076 fixou o limite por plano (Go 4.000, Pro 10.000). O manager passa a
-- poder trocar esse número numa stack específica — cortesia, negociação,
-- cliente que precisa de folga até o fim do ciclo. O override mora em
-- `stacks.request_quota_override` e é aplicado DENTRO de
-- `stack_request_quota`, a mesma função que o gateway usa para cortar: nem o
-- gateway nem o manager precisam saber que o override existe para respeitá-lo.
--
--   * NULL = padrão do plano (o comportamento da 0076).
--   * Inteiro > 0 = o limite da stack, no lugar do padrão. Zero não é aceito:
--     o gateway trata limite não positivo como "sem teto" (falha aberta, ver
--     request_quota.py), então 0 faria o contrário do que parece.
--   * Só vale para stacks de LLM, como a cota inteira. Imagem continua sem
--     teto mesmo com o campo preenchido.
--   * Muda o limite, não o ciclo nem a contagem: baixar o limite abaixo do
--     uso atual corta a stack na hora (dentro do cache de 60s do gateway).
--
-- As duas funções ganham `quota_default` (o limite do plano, sem override)
-- para o manager mostrar "personalizado" e o valor que "restaurar padrão"
-- devolve. Coluna nova no fim da tabela de retorno: quem lê por nome (gateway,
-- manager) não percebe a mudança.
--
-- Ordem de deploy: antes do painel. O painel antigo continua funcionando (só
-- ignora as colunas novas); o painel novo sem a migration mostra a cota sem
-- o botão de editar (quota_default ausente) e a action falha com erro claro.
--
-- Idempotente: pode rodar mais de uma vez.

begin;

alter table public.stacks
  add column if not exists request_quota_override integer;

alter table public.stacks
  drop constraint if exists stacks_request_quota_override_positive;
alter table public.stacks
  add constraint stacks_request_quota_override_positive
  check (request_quota_override is null or request_quota_override > 0);

comment on column public.stacks.request_quota_override is
  'Limite mensal de requisições desta stack no lugar do padrão do plano (0076/0077). NULL = padrão do plano.';

-- A tabela de retorno muda, então as duas precisam de drop (create or replace
-- não troca o tipo de retorno). A de todas as stacks chama a outra; cai junto.
drop function if exists public.stack_request_quotas(timestamptz);
drop function if exists public.stack_request_quota(uuid, timestamptz);

create function public.stack_request_quota(
  p_stack_id uuid,
  p_at timestamptz default now()
)
returns table (
  plan text,
  quota_limit integer,
  used bigint,
  cycle_start timestamptz,
  cycle_end timestamptz,
  quota_default integer
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
    (
      select count(*)
      from public.gateway_requests g
      -- left join: api_key_id vira nulo quando a chave é apagada (0066), e o
      -- uso dela continua sendo da stack
      left join public.api_keys k on k.id = g.api_key_id
      where g.stack_id = s.id
        and g.created_at >= c.cycle_start
        and g.created_at < c.cycle_end
        and k.purpose is distinct from 'playground'
    ),
    c.cycle_start,
    c.cycle_end,
    p.quota_default
  from public.stacks s
  cross join lateral (
    select case
      when coalesce(s.category, 'llm') <> 'llm' then null
      when s.plan in ('Go', 'VibeCoder') then 4000
      when s.plan = 'Pro' then 10000
      else null
    end as quota_default
  ) p
  cross join lateral public.stack_quota_cycle(s.purchase_date, p_at) c
  where s.id = p_stack_id
$$;

revoke all on function public.stack_request_quota(uuid, timestamptz) from public, anon;
grant execute on function public.stack_request_quota(uuid, timestamptz)
  to authenticated, service_role;

create function public.stack_request_quotas(p_at timestamptz default now())
returns table (
  stack_id uuid,
  plan text,
  quota_limit integer,
  used bigint,
  cycle_start timestamptz,
  cycle_end timestamptz,
  quota_default integer
)
language sql
stable
security invoker
set search_path = ''
as $$
  select s.id, q.plan, q.quota_limit, q.used, q.cycle_start, q.cycle_end, q.quota_default
  from public.stacks s
  cross join lateral public.stack_request_quota(s.id, p_at) q
$$;

revoke all on function public.stack_request_quotas(timestamptz) from public, anon, authenticated;
grant execute on function public.stack_request_quotas(timestamptz) to service_role;

commit;
