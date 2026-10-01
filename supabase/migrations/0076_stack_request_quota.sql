-- Cota mensal de requisições por stack (01/10/2026).
--
-- O TryStac anuncia "4.000 requisições/mês" no Go e "10.000 requisições/mês"
-- no Pro, e até aqui nada contava nem cortava isso. Esta migration é a fonte
-- única do número e da contagem: o gateway (que corta) e o manager (que
-- mostra o uso) leem de `stack_request_quota`, então o limite não existe
-- copiado em cada lugar. O painel do TryStac ainda não mostra a cota
-- (decisão de 01/10/2026); quando mostrar, lê daqui também.
--
-- ---------------------------------------------------------------------------
-- Regras (decisões comerciais de 01/10/2026)
-- ---------------------------------------------------------------------------
--   * Ciclo: mensal, ancorado no dia de `stacks.purchase_date` — inclusive no
--     plano anual, que paga 12 meses de uma vez mas tem cota por mês. Mês sem
--     aquele dia usa o último dia dele (compra em 31/01 renova em 28/02 e
--     volta a 31/03). Meia-noite UTC, a mesma régua de `purchase_date`
--     (default current_date do banco, que roda em UTC).
--   * O que conta: TODA linha de gateway_requests da stack no ciclo — qualquer
--     rota (inclusive /v1/models) e qualquer status. O que o gateway recusa
--     antes de registrar (401, 413, rate limit, inadimplência e o próprio 429
--     de cota) não gera linha e por isso não conta. /v1/messages/count_tokens
--     é métrica interna e nunca é registrado.
--   * Quem tem cota: só stacks de LLM, Go e Pro. Imagem, Max e Enterprise
--     devolvem quota_limit nulo (sem teto). "VibeCoder" é o nome antigo do Go,
--     mantido enquanto a 0049 não estiver em produção, como no gateway.
--   * Chaves de purpose = 'playground' (0044) ficam fora da soma, como já
--     ficam de account_token_usage_today e do painel (0024 do TryStac).
--
-- ---------------------------------------------------------------------------
-- Contagem direto em gateway_requests, sem tabela contadora
-- ---------------------------------------------------------------------------
-- O índice gateway_requests_stack_idx (stack_id, created_at desc) responde a
-- contagem do ciclo, que é no máximo ~10 mil linhas por stack; o gateway ainda
-- guarda o resultado por 60s. Um contador mantido por trigger seria mais uma
-- cópia do dado, e chaveado pelo ciclo ficaria errado no instante em que
-- alguém corrigisse `purchase_date` no banco — contando direto, mudar a data
-- só move a janela.
--
-- ---------------------------------------------------------------------------
-- Segurança
-- ---------------------------------------------------------------------------
-- `security invoker`, mesmo desenho das RPCs de uso do TryStac (0024 de lá):
-- quem isola tenant é a RLS de stacks/gateway_requests/api_keys, e o
-- `p_stack_id` de outro cliente devolve zero linhas. O gateway e o manager
-- chamam com service_role, que ignora RLS. `stack_quota_cycle` é só
-- aritmética de datas, mas precisa do grant porque roda com o privilégio de
-- quem chama a função de fora.
--
-- Ordem de deploy: só cria funções, então pode ir antes de tudo. O gateway que
-- a chama falha aberto se ela ainda não existir (ver request_quota.py).
--
-- Idempotente: pode rodar mais de uma vez.

begin;

drop function if exists public.stack_quota_cycle(date, timestamptz);
create function public.stack_quota_cycle(
  p_purchase_date date,
  p_at timestamptz
)
returns table (cycle_start timestamptz, cycle_end timestamptz)
language sql
stable
security invoker
set search_path = ''
as $$
  with b as (
    select
      (p_at at time zone 'UTC')::date as today,
      extract(day from p_purchase_date)::int as anchor,
      date_trunc('month', p_at at time zone 'UTC')::date as m0
  ), d as (
    -- dia de aniversário no mês anterior, no atual e no seguinte, já
    -- limitado ao último dia de cada um
    select
      today,
      (m0 - interval '1 month')::date
        + (least(anchor, extract(day from m0 - 1)::int) - 1) as s_prev,
      m0
        + (least(anchor, extract(day from (m0 + interval '1 month')::date - 1)::int) - 1) as s_this,
      (m0 + interval '1 month')::date
        + (least(anchor, extract(day from (m0 + interval '2 month')::date - 1)::int) - 1) as s_next
    from b
  )
  select
    (case when s_this <= today then s_this else s_prev end)::timestamp at time zone 'UTC',
    (case when s_this <= today then s_next else s_this end)::timestamp at time zone 'UTC'
  from d
$$;

revoke all on function public.stack_quota_cycle(date, timestamptz) from public, anon;
grant execute on function public.stack_quota_cycle(date, timestamptz)
  to authenticated, service_role;

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
  cycle_end timestamptz
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
      when s.plan in ('Go', 'VibeCoder') then 4000
      when s.plan = 'Pro' then 10000
      else null
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
    c.cycle_end
  from public.stacks s
  cross join lateral public.stack_quota_cycle(s.purchase_date, p_at) c
  where s.id = p_stack_id
$$;

revoke all on function public.stack_request_quota(uuid, timestamptz) from public, anon;
grant execute on function public.stack_request_quota(uuid, timestamptz)
  to authenticated, service_role;

-- Todas as stacks de uma vez, para a tabela de Stacks do manager não fazer uma
-- chamada por linha. Reaproveita stack_request_quota em vez de repetir a
-- regra: limite, ciclo e contagem continuam num lugar só. Só service_role —
-- o manager é o único consumidor, e o cliente final não tem por que listar
-- stacks de ninguém (a RLS devolveria só as dele, mas a superfície não
-- precisa existir).
drop function if exists public.stack_request_quotas(timestamptz);
create function public.stack_request_quotas(p_at timestamptz default now())
returns table (
  stack_id uuid,
  plan text,
  quota_limit integer,
  used bigint,
  cycle_start timestamptz,
  cycle_end timestamptz
)
language sql
stable
security invoker
set search_path = ''
as $$
  select s.id, q.plan, q.quota_limit, q.used, q.cycle_start, q.cycle_end
  from public.stacks s
  cross join lateral public.stack_request_quota(s.id, p_at) q
$$;

revoke all on function public.stack_request_quotas(timestamptz) from public, anon, authenticated;
grant execute on function public.stack_request_quotas(timestamptz) to service_role;

commit;
