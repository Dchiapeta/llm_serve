-- Uso repassado ao OpenRouter passa a contar em usage_metrics (01/10/2026).
--
-- usage_metrics só recebia o que o coletor do gateway puxa dos agents das
-- máquinas (collect_usage_metrics_once). Requisição atendida pelo OpenRouter
-- (0071/0073) nunca passa por agent nenhum, então ficava fora de tudo que lê
-- essa tabela: dashboard da stack no TryStac, "Minhas máquinas", páginas de
-- Stacks/Contas/CRM do painel e a cota diária (account_token_usage_today). O
-- dado já existia em gateway_requests (upstream = 'openrouter', com tokens).
--
-- ---------------------------------------------------------------------------
-- Como
-- ---------------------------------------------------------------------------
-- Trigger em gateway_requests: cada linha do OpenRouter soma 1 requisição e os
-- tokens dela numa linha de usage_metrics com machine_id NULO e
-- upstream = 'openrouter', agregada por (chave, stack, HORA). Agregada e não
-- uma linha por requisição porque vários leitores leem usage_metrics sem
-- paginar e bateriam no teto de 1000 linhas do PostgREST. Hora UTC pelo mesmo
-- motivo do image_usage_rollup (0067): mapeia 1:1 para a hora de São Paulo,
-- e os painéis dobram para dia em TypeScript.
--
-- Trigger, e não o gateway gravando: a linha de gateway_requests já é a
-- verdade da requisição (escrita no fim dela, com o usage do provedor), e o
-- uso fica atômico com ela — sem contador em memória que se perde num deploy.
--
-- Mesma régua do agent da máquina, que conta toda requisição que ele repassou
-- ao vLLM, com qualquer status: conta o que CHEGOU ao OpenRouter. Ficam fora
-- 503 (o gateway nem alcançou o provedor, ou não tinha chave para chamá-lo) e
-- 401/402 (problema da conta da Stac no OpenRouter — o cliente recebeu 503).
--
-- Machine_id nulo é o que mantém a separação certa nos leitores: as telas por
-- máquina (machines/[id], info do template) e o rateio de custo de GPU do CRM
-- filtram por machine_id e continuam só com uso de máquina; os totais por
-- stack/chave/conta passam a incluir o OpenRouter.
--
-- ---------------------------------------------------------------------------
-- O que NÃO passa a contar
-- ---------------------------------------------------------------------------
-- stack_usage_stats (classe de uso, 0032): ela decide como as stacks dividem a
-- VRAM das máquinas, e tráfego que nem passou pela máquina não pode empurrar
-- uma stack para "high". Recriada abaixo com `u.upstream is null` — o mesmo
-- resultado de antes desta migration.
--
-- ---------------------------------------------------------------------------
-- Histórico
-- ---------------------------------------------------------------------------
-- O backfill soma as requisições do OpenRouter já registradas (desde 29/09)
-- pela mesma régua. Roda uma vez só (quando a coluna `upstream` nasce). O
-- trigger é criado ANTES dele, na mesma transação: o lock do CREATE TRIGGER
-- segura os inserts concorrentes em gateway_requests até o commit, então
-- nenhuma requisição cai entre o backfill e o trigger, nem conta duas vezes.
--
-- Ordem de deploy: só a migration (o gateway não muda). Painel depois, para o
-- rótulo "OpenRouter" no gráfico por máquina do dashboard.

begin;

do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'usage_metrics'
      and column_name = 'upstream'
  ) then
    alter table public.usage_metrics alter column machine_id drop not null;
    alter table public.usage_metrics add column upstream text;

    -- chave do agregado por hora. Linha sem stack (chave avulsa) não casa
    -- com nada (NULL é distinto) e vira uma linha por requisição — raro, e
    -- inofensivo.
    create unique index usage_metrics_openrouter_hour_uidx
      on public.usage_metrics (api_key_id, stack_id, window_start)
      where upstream = 'openrouter';

    create function public.gateway_requests_openrouter_usage()
    returns trigger
    language plpgsql
    security definer
    set search_path = ''
    as $fn$
    begin
      insert into public.usage_metrics as um (
        api_key_id, stack_id, machine_id, upstream, window_start,
        requests, tokens_in, tokens_out, concurrent_peak
      )
      values (
        new.api_key_id, new.stack_id, null, 'openrouter',
        date_trunc('hour', new.created_at at time zone 'utc') at time zone 'utc',
        1, coalesce(new.tokens_in, 0), coalesce(new.tokens_out, 0), 0
      )
      on conflict (api_key_id, stack_id, window_start) where upstream = 'openrouter'
      do update set
        requests = um.requests + excluded.requests,
        tokens_in = um.tokens_in + excluded.tokens_in,
        tokens_out = um.tokens_out + excluded.tokens_out;
      return null;
    exception when others then
      -- a contagem nunca pode derrubar o log da requisição (o insert em
      -- gateway_requests falharia junto): perde-se o uso, fica o aviso
      raise warning 'usage_metrics do OpenRouter não gravado (gateway_request %): %',
        new.id, sqlerrm;
      return null;
    end;
    $fn$;

    create trigger gateway_requests_openrouter_usage
      after insert on public.gateway_requests
      for each row
      when (new.upstream = 'openrouter' and new.status_code not in (401, 402, 503))
      execute function public.gateway_requests_openrouter_usage();

    insert into public.usage_metrics (
      api_key_id, stack_id, machine_id, upstream, window_start,
      requests, tokens_in, tokens_out, concurrent_peak
    )
    select
      g.api_key_id, g.stack_id, null, 'openrouter',
      date_trunc('hour', g.created_at at time zone 'utc') at time zone 'utc',
      count(*), coalesce(sum(g.tokens_in), 0), coalesce(sum(g.tokens_out), 0), 0
    from public.gateway_requests g
    where g.upstream = 'openrouter'
      and g.status_code not in (401, 402, 503)
    group by g.api_key_id, g.stack_id,
             date_trunc('hour', g.created_at at time zone 'utc');
  end if;
end $$;

comment on column public.usage_metrics.upstream is
  'NULL = coletado do agent de uma máquina; ''openrouter'' = repasse, agregado por hora a partir de gateway_requests (0075)';

-- Classe de uso só com uso de MÁQUINA (ver cabeçalho). Mesma assinatura e
-- opções da 0066; `create or replace` preserva os grants.
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
    and u.upstream is null
  group by s.id, s.plan, s.usage_class, s.usage_class_updated_at,
           m.max_model_len, t.usage_class_config
$$;

commit;
