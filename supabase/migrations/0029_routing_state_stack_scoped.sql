-- #3 — Escopo de roteamento/adapter LoRA de ACCOUNT para STACK.
--
-- Antes: routing_state tinha account_id como PK e o adapter era carregado sob
-- o nome "acct-<account_id>" (docker/gateway/main.py lora_name). Numa conta com
-- múltiplas stacks, todas compartilhavam a mesma linha de rota E o mesmo nome de
-- adapter — uma stack podia servir com o fine-tune de outra stack da mesma conta
-- (system_prompt e RAG já eram por stack desde 0020/0021; o adapter e a rota não).
--
-- Depois: routing_state passa a ser escopado por STACK (PK = stack_id). O
-- account_id continua na tabela (denormalizado) para routing_history/logs/joins.
--
-- routing_state é estado EFÊMERO (o gateway o reconstrói via claim_route no
-- próximo request de cada stack). Por isso limpamos as linhas em vez de migrá-las
-- — uma conta multi-stack não tem mapeamento account->stack único (é justamente
-- o bug corrigido aqui). O custo é um re-load dos adapters no primeiro request
-- pós-deploy; fazer em janela de baixo tráfego (ver plano de rollout).
--
-- Migration IDEMPOTENTE: pode ser re-executada com segurança (guarda cada DDL).

-- 1. Limpa o estado efêmero (re-claim no próximo request).
delete from routing_state;

-- 2. Troca a chave primária de account_id para stack_id.
alter table routing_state drop constraint if exists routing_state_pkey;

alter table routing_state
  add column if not exists stack_id uuid references stacks(id) on delete cascade;

-- tabela vazia após o delete → set not null é seguro
alter table routing_state alter column stack_id set not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'routing_state_pkey'
  ) then
    alter table routing_state add constraint routing_state_pkey primary key (stack_id);
  end if;
end $$;

-- account_id deixa de ser PK mas continua obrigatório (denormalizado); index
-- para os joins/lookups por conta (history, métricas)
create index if not exists routing_state_account_idx on routing_state(account_id);
-- routing_state_machine_idx (migration 0005) permanece válido.

-- 3. claim_route agora chaveia por stack (lock de linha por stack_id serializa
--    requests concorrentes da MESMA stack; stacks distintas nunca colidem).
--    account_id entra junto para popular a coluna denormalizada no insert.
--    A assinatura antiga tinha 2 args (account_id, machine_id); a nova tem 3, o
--    que criaria um overload — droppar a antiga explicitamente evita ambiguidade.
drop function if exists claim_route(uuid, uuid);

create or replace function claim_route(p_stack_id uuid, p_account_id uuid, p_machine_id uuid)
returns table (
  stack_id uuid,
  account_id uuid,
  machine_id uuid,
  lora_adapter_id uuid,
  lora_status text,
  last_used_at timestamptz,
  updated_at timestamptz,
  claimed boolean
)
language plpgsql
security definer
as $$
#variable_conflict use_column
begin
  insert into routing_state (stack_id, account_id)
  values (p_stack_id, p_account_id)
  on conflict (stack_id) do nothing;

  return query
  update routing_state rs
     set machine_id = p_machine_id,
         lora_status = 'loading',
         updated_at = now()
   where rs.stack_id = p_stack_id
     and rs.lora_status = 'unloaded'
  returning rs.stack_id, rs.account_id, rs.machine_id, rs.lora_adapter_id,
            rs.lora_status, rs.last_used_at, rs.updated_at, true;

  -- outro chamador da mesma stack venceu a corrida (ou a rota já está ativa):
  -- devolve o estado atual com claimed = false
  if not found then
    return query
    select rs.stack_id, rs.account_id, rs.machine_id, rs.lora_adapter_id,
           rs.lora_status, rs.last_used_at, rs.updated_at, false
      from routing_state rs
     where rs.stack_id = p_stack_id;
  end if;
end $$;

-- 4. touch_route por stack. A assinatura antiga (p_account_id uuid) tem a MESMA
--    forma (1 arg uuid), então o Postgres não deixa renomear o parâmetro via
--    create or replace ("cannot change name of input parameter") — é preciso
--    droppar a função antiga antes de recriar com o novo nome de parâmetro.
drop function if exists touch_route(uuid);

create function touch_route(p_stack_id uuid)
returns void
language sql
security definer
as $$
  update routing_state set last_used_at = now() where stack_id = p_stack_id
$$;
