-- Adapters LoRA por conta, armazenados no Supabase Storage (bucket "loras").
-- storage_path guarda o prefixo dentro do bucket: {account_id}/{version}
-- O treino do adapter acontece fora deste sistema — aqui só registramos e
-- servimos adapters já existentes no bucket.
create table if not exists lora_adapters (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  storage_path text not null,
  version text not null,
  -- ready: apto a ser carregado. invalid: o load no vLLM falhou (adapter
  -- corrompido/incompatível) — setado pelo fluxo de serving (gateway/lifecycle)
  -- para a alocação parar de tentar servir este adapter.
  status text not null default 'ready', -- ready | invalid
  created_at timestamptz not null default now(),
  unique (account_id, version)
);

create index if not exists lora_adapters_account_idx on lora_adapters(account_id);

-- RLS habilitado sem policies: o painel acessa via service role; bloqueia acesso anônimo.
alter table lora_adapters enable row level security;
