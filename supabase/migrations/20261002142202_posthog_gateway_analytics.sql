-- Preparar/revisar antes do deploy; esta migration NÃO foi aplicada à nuvem.
-- Histórico anterior continua NULL: não inventamos conclusões antigas.
alter table public.gateway_requests
  add column analytics_origin text check (analytics_origin in ('customer', 'playground', 'unknown')),
  add column analytics_outcome text check (analytics_outcome in ('completed', 'error', 'aborted'));
-- As consultas diárias usam a data sem um filtro prévio de conta; o índice
-- parcial não inclui histórico, Playground ou chamadas sem analytics.
create index gateway_requests_analytics_day_idx on public.gateway_requests (created_at)
  where analytics_origin = 'customer' and analytics_outcome is not null;

create table public.gateway_analytics_outbox (
  id uuid primary key default gen_random_uuid(),
  resource_key text not null unique,
  event text not null check (event in ('first_inference_completed', 'api_usage_daily')),
  distinct_id uuid not null,
  event_timestamp timestamptz not null,
  properties jsonb not null,
  sent_at timestamptz,
  lease_until timestamptz,
  budget_month date,
  -- Linha que nunca deve ir ao PostHog: envelope inválido (não trava a fila)
  -- ou marcador de conta que já usava a API antes do tracking (dedupe).
  discarded_at timestamptz,
  discard_reason text check (discard_reason in ('preexisting_usage', 'invalid_envelope')),
  check ((discarded_at is null) = (discard_reason is null)),
  created_at timestamptz not null default now()
);
create index gateway_analytics_pending_idx on public.gateway_analytics_outbox (event_timestamp)
  where sent_at is null and discarded_at is null;
create table public.gateway_analytics_state (
  singleton boolean primary key default true check (singleton),
  next_day date not null default (now() at time zone 'UTC')::date,
  budget_month date,
  reserved_events integer not null default 0 check (reserved_events >= 0)
);
insert into public.gateway_analytics_state (singleton) values (true);

alter table public.gateway_analytics_outbox enable row level security;
alter table public.gateway_analytics_state enable row level security;
revoke all on public.gateway_analytics_outbox, public.gateway_analytics_state from public, anon, authenticated;
grant select, insert, update, delete on public.gateway_analytics_outbox, public.gateway_analytics_state to service_role;

-- SECURITY INVOKER: executada com o serviço que já grava o ledger; nenhuma
-- função privilegiada exposta ao browser. Não modifica autorização de contas.
create function public.queue_gateway_first_inference(account_uuid uuid) returns void
language plpgsql security invoker set search_path = '' as $$
declare identity record; first_row public.gateway_requests; preexisting boolean;
begin
  if exists (select 1 from public.gateway_analytics_outbox where resource_key = 'first:' || account_uuid::text)
    then return; end if;
  select * into first_row from public.gateway_requests
    where account_id = account_uuid and analytics_origin = 'customer' and analytics_outcome = 'completed'
      and path in ('chat/completions','completions','responses','messages','embeddings',
        'documents/extract','images/extract','images/generations','images/edits')
    order by created_at, id limit 1;
  if not found then return; end if;
  select a.user_id, coalesce(lower(btrim(a.email)) like '%@trystac.com', false) as internal
    into identity from public.accounts a where a.id = account_uuid;
  if identity.user_id is null then return; end if;
  -- Sucesso de cliente gravado sem os campos de analytics (antes da migration
  -- ou com a integração desligada) = a conta já tinha usado a API. Grava só o
  -- marcador descartado: dedupe sem enviar um falso "primeiro uso". Roda uma
  -- vez por conta; daí em diante o resource_key acima encerra cedo.
  select exists (
    select 1 from public.gateway_requests r
      left join public.api_keys k on k.id = r.api_key_id
    where r.account_id = account_uuid and r.analytics_origin is null
      and r.status_code between 200 and 299
      and coalesce(k.purpose, 'customer') = 'customer'
      and r.path in ('chat/completions','completions','responses','messages','embeddings',
        'documents/extract','images/extract','images/generations','images/edits')
  ) into preexisting;
  if preexisting then
    insert into public.gateway_analytics_outbox
      (resource_key, event, distinct_id, event_timestamp, properties, discarded_at, discard_reason)
    values ('first:' || account_uuid::text, 'first_inference_completed', identity.user_id,
      first_row.created_at, jsonb_build_object('account_id', account_uuid), now(), 'preexisting_usage')
    on conflict (resource_key) do nothing;
    return;
  end if;
  insert into public.gateway_analytics_outbox (resource_key, event, distinct_id, event_timestamp, properties)
  values (
    'first:' || account_uuid::text, 'first_inference_completed', identity.user_id, first_row.created_at,
    jsonb_strip_nulls(jsonb_build_object(
      'account_id', account_uuid, 'stack_id', first_row.stack_id, 'path', first_row.path,
      'model', case when first_row.model ~ '^[A-Za-z0-9_.:/-]{1,120}$' and position('://' in first_row.model) = 0
        then first_row.model else 'unknown' end,
      'duration_ms', first_row.duration_ms, 'tokens_in', first_row.tokens_in, 'tokens_out', first_row.tokens_out,
      '$internal_or_test_user', identity.internal
    ))
  ) on conflict (resource_key) do nothing;
end $$;
revoke all on function public.queue_gateway_first_inference(uuid) from public, anon, authenticated;
grant execute on function public.queue_gateway_first_inference(uuid) to service_role;

create function public.capture_gateway_first_inference() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if new.analytics_origin = 'customer' and new.analytics_outcome = 'completed' then
    perform public.queue_gateway_first_inference(new.account_id);
  end if;
  return new;
exception when others then
  -- Subtransaction: falha SOMENTE da analytics não desfaz o ledger. O
  -- exportador reconcilia o primeiro fato novamente a partir do ledger.
  raise warning 'gateway analytics first-use fact unavailable (SQLSTATE %)', sqlstate;
  return new;
end $$;
revoke all on function public.capture_gateway_first_inference() from public, anon, authenticated;
grant execute on function public.capture_gateway_first_inference() to service_role;
create trigger gateway_first_inference after insert on public.gateway_requests
  for each row execute function public.capture_gateway_first_inference();

-- Janelas fechadas: created_at é a gravação durável no ledger. O checkpoint
-- avança na MESMA transação da outbox, até ontem UTC, com 2h de margem.
-- Não há snapshot parcial, nem amostragem de contadores.
create function public.prepare_gateway_analytics_exports() returns integer
language plpgsql security invoker set search_path = '' as $$
declare day date; last_closed date; inserted integer; total integer := 0; account_uuid uuid;
begin
  select next_day into day from public.gateway_analytics_state where singleton for update;
  last_closed := (now() at time zone 'UTC' - interval '2 hours')::date - 1;
  delete from public.gateway_analytics_outbox
    where event = 'api_usage_daily' and sent_at < now() - interval '90 days';
  -- Limite de trabalho por tick: uma semana; backlog retoma no próximo tick.
  for i in 1..7 loop
    exit when day > last_closed;
    for account_uuid in select distinct r.account_id from public.gateway_requests r
      where r.created_at >= day::timestamp at time zone 'UTC'
        and r.created_at < (day + 1)::timestamp at time zone 'UTC'
        and r.analytics_origin = 'customer' and r.analytics_outcome = 'completed'
        and r.account_id is not null
    loop
      perform public.queue_gateway_first_inference(account_uuid);
    end loop;
    insert into public.gateway_analytics_outbox (resource_key, event, distinct_id, event_timestamp, properties)
    select 'daily:' || a.id::text || ':' || day::text || ':' || g.safe_model,
      'api_usage_daily', a.user_id, day::timestamp at time zone 'UTC',
      jsonb_build_object(
        'account_id', a.id, 'model', g.safe_model,
        'period_start', day::timestamp at time zone 'UTC',
        'period_end', (day + 1)::timestamp at time zone 'UTC',
        'request_count', count(*),
        'success_count', count(*) filter (where g.analytics_outcome = 'completed'),
        'error_count', count(*) filter (where g.analytics_outcome = 'error'),
        'aborted_count', count(*) filter (where g.analytics_outcome = 'aborted'),
        'stream_count', count(*) filter (where g.stream),
        'tokens_in', coalesce(sum(g.tokens_in) filter (where g.tokens_in >= 0), 0),
        'tokens_out', coalesce(sum(g.tokens_out) filter (where g.tokens_out >= 0), 0),
        'tokens_known_count', count(*) filter (where g.tokens_in >= 0 and g.tokens_out >= 0),
        'duration_ms_sum', coalesce(sum(g.duration_ms) filter (where g.duration_ms >= 0), 0),
        'duration_ms_p95', percentile_cont(0.95) within group (order by g.duration_ms)
          filter (where g.duration_ms >= 0),
        'duration_known_count', count(*) filter (where g.duration_ms >= 0),
        'reported_cost_usd_sum', coalesce(sum(g.cost_usd) filter (where g.cost_usd >= 0), 0),
        'cost_known_count', count(*) filter (where g.cost_usd >= 0),
        '$internal_or_test_user', coalesce(lower(btrim(a.email)) like '%@trystac.com', false)
      )
    from (
      select r.*, case when r.model ~ '^[A-Za-z0-9_.:/-]{1,120}$' and position('://' in r.model) = 0
        then r.model else 'unknown' end as safe_model
      from public.gateway_requests r
      where r.created_at >= day::timestamp at time zone 'UTC'
        and r.created_at < (day + 1)::timestamp at time zone 'UTC'
        and r.analytics_origin = 'customer' and r.analytics_outcome is not null
        and r.path in ('chat/completions','completions','responses','messages','embeddings',
          'documents/extract','images/extract','images/generations','images/edits')
    ) g join public.accounts a on a.id = g.account_id
    where a.user_id is not null
    group by a.id, a.user_id, a.email, g.safe_model
    on conflict (resource_key) do nothing;
    get diagnostics inserted = row_count;
    total := total + inserted;
    day := day + 1;
    update public.gateway_analytics_state set next_day = day where singleton;
  end loop;
  return total;
end $$;
revoke all on function public.prepare_gateway_analytics_exports() from public, anon, authenticated;
grant execute on function public.prepare_gateway_analytics_exports() to service_role;

-- Lease + orçamento atômico entre réplicas. Reservar o mesmo UUID novamente
-- no mesmo mês não gasta outra unidade do limite local. Um ack perdido
-- reenvia o envelope exato (UUID/event/distinct_id/timestamp).
create function public.claim_gateway_analytics_exports(monthly_limit integer default 200000, batch_size integer default 100)
returns setof public.gateway_analytics_outbox
language plpgsql security invoker set search_path = '' as $$
declare state public.gateway_analytics_state; item public.gateway_analytics_outbox;
  month date := date_trunc('month', now() at time zone 'UTC')::date; taken integer := 0;
begin
  if monthly_limit <= 0 or batch_size <= 0 then return; end if;
  select * into state from public.gateway_analytics_state where singleton for update;
  if state.budget_month is distinct from month then
    state.budget_month := month; state.reserved_events := 0;
  end if;
  for item in select * from public.gateway_analytics_outbox
    where sent_at is null and discarded_at is null and (lease_until is null or lease_until < now())
    order by case when event = 'first_inference_completed' then 0 else 1 end, event_timestamp, id
    for update skip locked limit 500
  loop
    exit when taken >= greatest(0, least(batch_size, 100));
    if item.budget_month is distinct from month then
      if state.reserved_events >= greatest(0, least(monthly_limit, 200000)) then continue; end if;
      state.reserved_events := state.reserved_events + 1;
    end if;
    update public.gateway_analytics_outbox set lease_until = now() + interval '5 minutes', budget_month = month
      where id = item.id returning * into item;
    taken := taken + 1;
    return next item;
  end loop;
  update public.gateway_analytics_state set budget_month = state.budget_month, reserved_events = state.reserved_events
    where singleton;
end $$;
revoke all on function public.claim_gateway_analytics_exports(integer,integer) from public, anon, authenticated;
grant execute on function public.claim_gateway_analytics_exports(integer,integer) to service_role;
