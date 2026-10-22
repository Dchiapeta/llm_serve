-- Request ponderada por tokens na cota mensal (15/10/2026).
--
-- Go e Pro passam a ter as mesmas features (contexto de 256K e código
-- incluídos — ver docker/gateway/plan_unification.py), e a única diferença
-- entre eles é a cota: Go 4.000, Pro 10.000 (0076/0077, sem mudança). Com
-- contexto grande, uma requisição de agente de código custa no OpenRouter
-- várias vezes o que uma de chat custa, então a requisição passa a valer pelo
-- tamanho da entrada:
--
--   peso = ceil(tokens_in / 32768), mínimo 1
--
-- Até 32K tokens de entrada conta 1, como antes — chat e n8n não mudam.
-- Linha sem tokens_in (erro antes do upstream, rota sem usage) vale 1.
--
-- ---------------------------------------------------------------------------
-- O que muda e o que NÃO muda agora
-- ---------------------------------------------------------------------------
--   * `stack_request_quota` ganha `used_weighted`, a soma dos pesos no ciclo.
--     É o que o gateway passa a usar para decidir o corte (request_quota.py);
--     com REQUEST_QUOTA_ENFORCE desligado, só registra no log.
--   * `used` continua sendo a contagem crua. Hoje o painel do cliente no
--     TryStac mostra esse número; junto com o corte, ele passa a mostrar
--     `used_weighted` como "requisições usadas" — sem nunca exibir o peso —,
--     para a barra, os avisos de 80/95/100% e o bloqueio baterem no mesmo
--     número. O `used` cru fica para análise interna (manager).
--   * `request_weight_report` alimenta a página "Cota ponderada" do manager:
--     distribuição de tokens por requisição, linhas sem tokens_in e o peso
--     resultante, por plano e por upstream (máquina própria x OpenRouter).
--
-- Mesmas regras da 0076 para o que conta (toda linha de gateway_requests da
-- stack no ciclo, fora as chaves de playground) e para quem tem cota.
--
-- Colunas novas no fim das tabelas de retorno: quem lê por nome (gateway,
-- manager, TryStac) não percebe a mudança.
--
-- Ordem de deploy: antes do gateway. Gateway antigo ignora `used_weighted`;
-- gateway novo sem esta migration cai no `used` cru.
--
-- Idempotente: pode rodar mais de uma vez.

begin;

-- Espelha request_quota.REQUEST_WEIGHT_TOKENS no gateway.
create or replace function public.request_weight(p_tokens_in integer)
returns integer
language sql
immutable
parallel safe
set search_path = ''
as $$
  select case
    when p_tokens_in is null or p_tokens_in <= 0 then 1
    else greatest(1, ceil(p_tokens_in / 32768.0)::integer)
  end
$$;

grant execute on function public.request_weight(integer)
  to authenticated, service_role;

-- A tabela de retorno muda, então as duas precisam de drop (mesmo motivo da
-- 0077). A de todas as stacks chama a outra; cai junto.
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
      when s.plan in ('Go', 'VibeCoder') then 4000
      when s.plan = 'Pro' then 10000
      else null
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

create function public.stack_request_quotas(p_at timestamptz default now())
returns table (
  stack_id uuid,
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
  select s.id, q.plan, q.quota_limit, q.used, q.cycle_start, q.cycle_end,
         q.quota_default, q.used_weighted
  from public.stacks s
  cross join lateral public.stack_request_quota(s.id, p_at) q
$$;

revoke all on function public.stack_request_quotas(timestamptz) from public, anon, authenticated;
grant execute on function public.stack_request_quotas(timestamptz) to service_role;

-- Distribuição para a página de análise do manager. Uma linha por
-- (plano, upstream, faixa de tokens_in). Mesmo recorte da cota: stacks de
-- LLM, sem chave de playground. O plano é o ATUAL da stack — uma stack que
-- trocou de plano no período aparece inteira no plano novo.
create or replace function public.request_weight_report(
  p_since timestamptz default now() - interval '30 days'
)
returns table (
  plan text,
  upstream text,
  bucket text,
  requests bigint,
  weighted bigint
)
language sql
stable
security invoker
set search_path = ''
as $$
  select
    s.plan,
    coalesce(g.upstream, 'machine'),
    case
      when g.tokens_in is null then 'none'
      when g.tokens_in <= 8192 then 'le_8k'
      when g.tokens_in <= 32768 then 'le_32k'
      when g.tokens_in <= 65536 then 'le_64k'
      when g.tokens_in <= 131072 then 'le_128k'
      when g.tokens_in <= 262144 then 'le_256k'
      else 'gt_256k'
    end,
    count(*),
    coalesce(sum(public.request_weight(g.tokens_in)), 0)::bigint
  from public.gateway_requests g
  join public.stacks s on s.id = g.stack_id
  left join public.api_keys k on k.id = g.api_key_id
  where g.created_at >= p_since
    and coalesce(s.category, 'llm') = 'llm'
    and k.purpose is distinct from 'playground'
  group by 1, 2, 3
$$;

revoke all on function public.request_weight_report(timestamptz) from public, anon, authenticated;
grant execute on function public.request_weight_report(timestamptz) to service_role;

commit;
