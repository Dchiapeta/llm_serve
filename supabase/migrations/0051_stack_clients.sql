-- Quantos LUGARES distintos conectam numa stack.
--
-- Até aqui o plano limitava capacidade (slots de máquina), vazão (rate limit)
-- e volume (cota diária de tokens), mas nada impedia o cliente de colar a
-- mesma chave em 20 máquinas: uma chave Go valia por uma equipe inteira.
--
-- Esta tabela é a segunda de DUAS camadas, e a mais fraca das duas:
--
--   1. Chaves ativas por stack (MAX_KEYS_BY_PLAN, lib/types.ts) — o contrato.
--      Exato, sem adivinhação, aplicado em createKey.
--   2. Ambientes distintos (esta tabela) — o detector de quem furou o
--      contrato usando UMA chave em todo canto.
--
-- (2) é aproximado POR CONSTRUÇÃO: nenhum cliente do produto (Claude Code,
-- Cursor, Codex) manda identificador de máquina, e não há sessão nem cookie
-- numa API. O fingerprint é derivado de User-Agent + bloco de rede — os dois
-- controlados pelo cliente. Serve pra tornar compartilhamento em massa
-- inconveniente; nunca é controle de segurança. Ver o docstring de
-- docker/gateway/client_identity.py.
--
-- A unicidade é por STACK, não por chave: fosse por chave, o cliente emitiria
-- uma chave nova por ambiente e zeraria a contagem — exatamente o que a
-- camada 1 já limita, deixando esta sem função.
--
-- ORDEM DE DEPLOY: aplicar ANTES do gateway. Diferente da 0050, uma coluna
-- faltando aqui não derruba o tráfego (touch_stack_client é fail-open e
-- best-effort), mas a RPC ausente gera um 404 por ambiente novo em todo
-- request frio até a migration rodar.
--
-- Idempotente: pode rodar mais de uma vez.

create table if not exists stack_clients (
  id uuid primary key default gen_random_uuid(),
  stack_id uuid not null references stacks(id) on delete cascade,
  -- FK nullable + on delete set null: mesmo padrão de gateway_requests
  -- (0038) — o histórico de ambientes sobrevive à conta/chave sumindo.
  account_id uuid references accounts(id) on delete set null,
  -- ÚLTIMA chave vista neste ambiente, não a única: o mesmo lugar pode
  -- rodar Claude Code com uma chave e um script com outra.
  api_key_id uuid references api_keys(id) on delete set null,
  fingerprint text not null,
  client_label text,
  -- cru e truncado, pelo mesmo motivo de gateway_requests.user_agent (0041):
  -- registrar sim, confiar não.
  user_agent text,
  -- BLOCO de rede ("189.45.12.0/24"), nunca o IP completo. IP cheio é dado
  -- pessoal identificável; o bloco basta pra separar casa de escritório e é
  -- o que sobrevive à renovação de lease do ISP.
  ip_bucket text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  requests bigint not null default 0,
  -- 'released' = o cliente liberou a vaga no painel. Não apagamos a linha:
  -- o histórico ("este ambiente já existiu") é o que permite distinguir
  -- troca de notebook de compartilhamento crescente.
  status text not null default 'active',
  released_at timestamptz,
  unique (stack_id, fingerprint)
);

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'stack_clients_status_valid'
  ) then
    alter table stack_clients
      add constraint stack_clients_status_valid
      check (status in ('active', 'released'));
  end if;
end $$;

-- Serve as duas leituras: a contagem da janela deslizante (caminho frio do
-- gateway) e a listagem do painel, que ordena por uso mais recente.
create index if not exists stack_clients_stack_idx
  on stack_clients (stack_id, last_seen_at desc);

alter table stack_clients enable row level security;

-- Registra o ambiente e decide, na MESMA transação, se ele tem vaga.
--
-- A decisão mora aqui e não no Python de propósito: rate_buckets e in_flight
-- (docker/gateway/main.py) são estado de réplica única e já não sobrevivem a
-- escalar o gateway horizontalmente. Este limite não repete o erro — o estado
-- autoritativo é a tabela, e o advisory lock serializa o caminho frio por
-- stack, então duas réplicas admitindo ambientes novos ao mesmo tempo não
-- conseguem furar o teto.
--
-- p_cap NULL = sem teto (Enterprise), mesma convenção de machine_high_cap
-- (0037) e de RAG_FILE_LIMIT_BY_PLAN (lib/types.ts).
--
-- Vaga é por JANELA DESLIZANTE (p_window_days): ambiente parado libera a vaga
-- sozinho. Sem isso, trocar de notebook viraria ticket de suporte — e uma
-- linha 'active' vencida disputa vaga de novo em vez de ocupá-la de graça.
--
-- Ambiente negado NÃO é gravado: a linha só existe pra quem ocupa vaga, então
-- a contagem da tabela é sempre a contagem real de lugares admitidos.
create or replace function touch_stack_client(
  p_stack_id uuid,
  p_account_id uuid,
  p_api_key_id uuid,
  p_fingerprint text,
  p_label text,
  p_user_agent text,
  p_ip_bucket text,
  p_window_days integer,
  p_cap integer
)
returns table (admitted boolean, used integer)
language plpgsql
security definer
as $$
declare
  v_cutoff timestamptz := now() - make_interval(days => greatest(p_window_days, 1));
  v_used integer;
begin
  -- Caminho quente: ambiente conhecido, ativo e DENTRO da janela. Já ocupa
  -- vaga, então não disputa nada — só atualiza o relógio.
  update stack_clients
     set last_seen_at = now(),
         requests     = requests + 1,
         api_key_id   = coalesce(p_api_key_id, api_key_id),
         user_agent   = coalesce(p_user_agent, user_agent),
         client_label = coalesce(p_label, client_label),
         ip_bucket    = coalesce(p_ip_bucket, ip_bucket)
   where stack_id      = p_stack_id
     and fingerprint   = p_fingerprint
     and status        = 'active'
     and last_seen_at  > v_cutoff;

  if found then
    select count(*)::integer into v_used
      from stack_clients
     where stack_id = p_stack_id
       and status = 'active'
       and last_seen_at > v_cutoff;
    return query select true, v_used;
    return;
  end if;

  -- Caminho frio: ambiente novo, liberado pelo painel, ou conhecido mas
  -- vencido. Todos disputam vaga em pé de igualdade.
  perform pg_advisory_xact_lock(hashtext(p_stack_id::text));

  select count(*)::integer into v_used
    from stack_clients
   where stack_id = p_stack_id
     and status = 'active'
     and last_seen_at > v_cutoff;

  if p_cap is not null and v_used >= p_cap then
    return query select false, v_used;
    return;
  end if;

  insert into stack_clients (
    stack_id, account_id, api_key_id, fingerprint,
    client_label, user_agent, ip_bucket, requests
  )
  values (
    p_stack_id, p_account_id, p_api_key_id, p_fingerprint,
    p_label, p_user_agent, p_ip_bucket, 1
  )
  on conflict (stack_id, fingerprint) do update
     set status       = 'active',
         released_at  = null,
         last_seen_at = now(),
         requests     = stack_clients.requests + 1,
         account_id   = coalesce(excluded.account_id, stack_clients.account_id),
         api_key_id   = coalesce(excluded.api_key_id, stack_clients.api_key_id),
         user_agent   = coalesce(excluded.user_agent, stack_clients.user_agent),
         client_label = coalesce(excluded.client_label, stack_clients.client_label),
         ip_bucket    = coalesce(excluded.ip_bucket, stack_clients.ip_bucket);

  return query select true, v_used + 1;
end $$;

-- Mesma trava da next_stack_machine_name (0017), e aqui ela é obrigatória:
-- security definer + ESCRITA. Sem o revoke, qualquer portador da anon key
-- chamaria /rpc/touch_stack_client com um p_stack_id alheio e um fingerprint
-- inventado por chamada, enchendo o teto de ambientes de um cliente pagante
-- até ele levar 403 — negação de serviço a um POST de distância.
revoke execute on function touch_stack_client(
  uuid, uuid, uuid, text, text, text, text, integer, integer
) from public, anon, authenticated;
grant execute on function touch_stack_client(
  uuid, uuid, uuid, text, text, text, text, integer, integer
) to service_role;
