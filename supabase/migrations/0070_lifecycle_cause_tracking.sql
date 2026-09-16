-- Rastreamento de causa: por que uma máquina subiu, e por que NÃO subiu.
--
-- Motivação: 16/09/2026, llm-stack-505 nasceu por try_provision_for_request a
-- partir de resolve_base_machine (docker/gateway/main.py) e nunca se descobriu
-- qual chave disparou -- machine_events só tinha (machine_id, type, message
-- livre) e o 503 pré-rota que precedeu a criação não chega em gateway_requests
-- (escopo da 0038: só o que passou de autenticação e resolução de rota).
--
-- Duas peças:
--   1. machine_events ganha estrutura (cause/actor/trigger_meta/trace_id) sem
--      perder `message` -- o card do dashboard segue lendo message.
--   2. provision_decisions: tabela nova pras DECISÕES, inclusive as negadas e
--      os 503 servidos sem criar nada. Separada de machine_events porque o
--      volume é outra ordem de grandeza (uma rajada de requests em cooldown
--      gera uma linha por request) e porque negação não tem machine_id -- é
--      fato do pool (plan, category), não da máquina.
--
-- Vocabulário fechado de `cause` em docker/gateway/trigger_ctx.py e os rótulos
-- em components/machines/cause-labels.ts.
--
-- Idempotente.

begin;

-- ---------- 1. machine_events estruturado ----------

alter table public.machine_events
  add column if not exists cause text,
  add column if not exists actor text,          -- request | lifecycle | admin | gateway | panel
  add column if not exists trigger_meta jsonb,  -- SÓ metadado (ver trigger_ctx.py)
  add column if not exists trace_id uuid,       -- = X-Stac-Request-Id
  add column if not exists machine_label text;  -- nome no instante do evento

-- `trigger_meta` e não `trigger`: TRIGGER é keyword do Postgres (non-reserved,
-- funcionaria sem aspas, mas não vale a ambiguidade em filtros do PostgREST
-- e em quem ler o schema depois).
comment on column public.machine_events.trigger_meta is
  'Metadado do originador: api_key_id, key_prefix, account_id, stack_id, plan, '
  'category, path, user_agent, reason, admin_email. NUNCA conteúdo de prompt '
  'nem chave em claro.';

-- FK cascade -> set null. terminateMachine faz UPDATE (não DELETE), então nada
-- foi perdido ainda; mas um DELETE futuro levaria junto a forense inteira.
-- Mesmo contrato de gateway_requests (0038) e usage_metrics (0066). A 0001 não
-- nomeia a constraint (veio do default), então localiza por catálogo.
do $$
declare c record;
begin
  for c in
    select con.conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
    join pg_class frel on frel.oid = con.confrelid
    where nsp.nspname = 'public' and rel.relname = 'machine_events'
      and frel.relname = 'machines' and con.contype = 'f'
  loop
    execute format('alter table public.machine_events drop constraint %I', c.conname);
  end loop;
end $$;

alter table public.machine_events
  add constraint machine_events_machine_id_fkey
  foreign key (machine_id) references public.machines(id) on delete set null;

-- A timeline por máquina não existia: o único índice era por created_at.
create index if not exists machine_events_machine_idx
  on public.machine_events(machine_id, created_at desc);
create index if not exists machine_events_cause_idx
  on public.machine_events(cause, created_at desc) where cause is not null;
create index if not exists machine_events_trace_idx
  on public.machine_events(trace_id) where trace_id is not null;

-- ---------- 2. Decisões do ciclo de vida ----------

create table if not exists public.provision_decisions (
  id uuid primary key default gen_random_uuid(),
  outcome text not null,              -- granted | denied | served_503
  cause text not null,                -- vocabulário fechado (trigger_ctx.py)
  actor text not null default 'gateway',
  plan text,
  category text,
  -- FKs nullable + set null: o diagnóstico sobrevive à conta/stack/chave sumir.
  machine_id uuid references public.machines(id) on delete set null,
  account_id uuid references public.accounts(id) on delete set null,
  stack_id   uuid references public.stacks(id)   on delete set null,
  api_key_id uuid references public.api_keys(id) on delete set null,
  key_prefix text,                    -- sobrevive ao set null acima
  trace_id uuid,
  trigger_meta jsonb,
  repeat_count integer not null default 1,  -- linhas suprimidas pelo dedupe
  created_at timestamptz not null default now()
);

create index if not exists provision_decisions_created_idx
  on public.provision_decisions(created_at desc);
create index if not exists provision_decisions_pool_idx
  on public.provision_decisions(plan, category, created_at desc);
create index if not exists provision_decisions_key_idx
  on public.provision_decisions(api_key_id, created_at desc);
create index if not exists provision_decisions_stack_idx
  on public.provision_decisions(stack_id, created_at desc);
create index if not exists provision_decisions_trace_idx
  on public.provision_decisions(trace_id) where trace_id is not null;

-- RLS ligada sem policy: só service role (mesmo padrão de machine_events).
-- IMPORTANTE: esta tabela NÃO deve ganhar policy de leitura por stack -- ela
-- expõe decisões de capacidade de outros clientes do mesmo pool.
alter table public.provision_decisions enable row level security;

commit;
