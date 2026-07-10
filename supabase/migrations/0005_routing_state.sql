-- Estado de roteamento: em qual máquina está o adapter LoRA de cada conta.
-- Fonte da verdade consultada pelo gateway a cada request; escrita apenas
-- pela camada de acesso (lib/routing.ts no painel, routing.py no gateway).
create table if not exists routing_state (
  account_id uuid primary key references accounts(id) on delete cascade,
  machine_id uuid references machines(id) on delete set null,
  lora_adapter_id uuid references lora_adapters(id) on delete set null,
  -- unloaded: sem adapter em VRAM (livre para claim)
  -- loading:  claim feito, adapter subindo pela primeira vez
  -- loaded:   servindo normalmente em machine_id
  -- migrating: em migração; machine_id continua apontando para a ORIGEM
  --            (que segue 100% funcional) até o load no destino confirmar
  lora_status text not null default 'unloaded',
  last_used_at timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists routing_state_machine_idx on routing_state(machine_id);

-- RLS habilitado sem policies: acesso só via service role.
alter table routing_state enable row level security;

-- Claim atômico: garante que apenas UM chamador inicia o load do adapter de
-- uma conta, mesmo com requests concorrentes. Dois passos na mesma transação:
-- 1) garante a linha; 2) update condicionado a lora_status='unloaded' — o
-- lock de linha do Postgres serializa chamadores concorrentes e o perdedor
-- reavalia o WHERE já vendo 'loading' (claimed = false).
create or replace function claim_route(p_account_id uuid, p_machine_id uuid)
returns table (
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
-- os nomes do "returns table" viram variáveis plpgsql e colidem com as colunas
-- (ex: "account_id is ambiguous" no on conflict); esta pragma resolve toda
-- ambiguidade a favor da COLUNA, mantendo os nomes de saída que o PostgREST expõe
#variable_conflict use_column
begin
  insert into routing_state (account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  return query
  update routing_state rs
     set machine_id = p_machine_id,
         lora_status = 'loading',
         updated_at = now()
   where rs.account_id = p_account_id
     and rs.lora_status = 'unloaded'
  returning rs.account_id, rs.machine_id, rs.lora_adapter_id, rs.lora_status,
            rs.last_used_at, rs.updated_at, true;

  -- outro chamador venceu a corrida (ou a rota já está ativa):
  -- devolve o estado atual com claimed = false
  if not found then
    return query
    select rs.account_id, rs.machine_id, rs.lora_adapter_id, rs.lora_status,
           rs.last_used_at, rs.updated_at, false
      from routing_state rs
     where rs.account_id = p_account_id;
  end if;
end $$;

-- Marca uso recente (chamado pelo gateway, throttled do lado dele).
create or replace function touch_route(p_account_id uuid)
returns void
language sql
security definer
as $$
  update routing_state set last_used_at = now() where account_id = p_account_id
$$;
