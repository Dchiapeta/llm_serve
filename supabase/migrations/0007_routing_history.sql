-- Histórico de alocação/migração de máquina por conta. routing_state só
-- guarda o estado atual; esta tabela é o log — um registro por alocação
-- (claim), migração ou liberação de rota.
create table if not exists routing_history (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  machine_id uuid references machines(id) on delete set null,
  from_machine_id uuid references machines(id) on delete set null,
  lora_adapter_id uuid references lora_adapters(id) on delete set null,
  -- allocated: claim_route venceu a corrida e alocou a conta numa máquina
  -- migrated:  conta movida de from_machine_id para machine_id
  -- released:  slot liberado (mark_slot_idle), sem máquina
  event text not null,
  created_at timestamptz not null default now()
);

create index if not exists routing_history_account_idx
  on routing_history(account_id, created_at desc);

alter table routing_history enable row level security;
