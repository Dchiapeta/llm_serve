-- Schema inicial do RunPod LLM Manager

create table if not exists templates (
  id uuid primary key default gen_random_uuid(),
  runpod_template_id text,
  name text not null,
  image text not null,
  model_name text not null,
  gpu_types text[] not null default '{}',
  env jsonb not null default '{}',
  disk_gb integer not null default 40,
  -- parâmetros para cálculo de capacidade
  model_footprint_gb numeric not null default 16,
  kv_reserve_gb_per_user numeric not null default 2,
  created_at timestamptz not null default now()
);

create table if not exists machines (
  id uuid primary key default gen_random_uuid(),
  runpod_pod_id text unique,
  name text not null,
  gpu_type text not null,
  status text not null default 'creating', -- creating | running | stopped | terminated | error
  template_id uuid references templates(id) on delete set null,
  admin_secret text not null,
  model_name text,
  vram_gb numeric,
  cost_per_hr numeric,
  public_url text,
  created_at timestamptz not null default now()
);

create table if not exists accounts (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text,
  created_at timestamptz not null default now()
);

create table if not exists api_keys (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  machine_id uuid not null references machines(id) on delete cascade,
  key_hash text not null,
  key_prefix text not null,
  status text not null default 'active', -- active | revoked
  created_at timestamptz not null default now()
);

create index if not exists api_keys_machine_idx on api_keys(machine_id);
create index if not exists api_keys_account_idx on api_keys(account_id);

create table if not exists usage_metrics (
  id uuid primary key default gen_random_uuid(),
  api_key_id uuid references api_keys(id) on delete cascade,
  machine_id uuid not null references machines(id) on delete cascade,
  window_start timestamptz not null,
  requests bigint not null default 0,
  tokens_in bigint not null default 0,
  tokens_out bigint not null default 0,
  concurrent_peak integer not null default 0,
  unique (api_key_id, machine_id, window_start)
);

create index if not exists usage_metrics_machine_idx on usage_metrics(machine_id, window_start desc);
create index if not exists usage_metrics_key_idx on usage_metrics(api_key_id, window_start desc);

create table if not exists machine_events (
  id uuid primary key default gen_random_uuid(),
  machine_id uuid references machines(id) on delete cascade,
  type text not null, -- created | started | stopped | terminated | key_created | key_revoked | sync | error
  message text not null,
  created_at timestamptz not null default now()
);

create index if not exists machine_events_created_idx on machine_events(created_at desc);

-- RLS: o painel acessa via service role; bloqueia acesso anônimo
alter table templates enable row level security;
alter table machines enable row level security;
alter table accounts enable row level security;
alter table api_keys enable row level security;
alter table usage_metrics enable row level security;
alter table machine_events enable row level security;
