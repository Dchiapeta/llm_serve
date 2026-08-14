-- Log de requisições individuais feitas ao gateway (chat/completions,
-- completions, embeddings, responses, models, /v1/messages). Antes desta migration
-- não existia nenhum registro por-requisição — usage_metrics é agregado por
-- janela de ~2min (window_start), então não dá pra responder "quais
-- requisições foram feitas, quando, por qual stack/chave, com qual status".
--
-- FK nullable + on delete set null em todas: mesmo padrão de
-- stacks.machine_id (0013), api_keys.stack_id (0019) e usage_metrics.stack_id
-- (0036) — o histórico de requisições sobrevive à conta/stack/chave/máquina
-- sendo removida depois.
--
-- Escrito pelo gateway de forma fire-and-forget (spawn_tracked) no
-- fechamento de cada requisição — só as que passam de autenticação e
-- resolução de rota (mesmo escopo hoje coberto por in_flight/release_flight
-- em docker/gateway/main.py); rejeições precoces (401/429/503) não geram
-- linha.
create table if not exists gateway_requests (
  id uuid primary key default gen_random_uuid(),
  account_id uuid references accounts(id) on delete set null,
  stack_id uuid references stacks(id) on delete set null,
  api_key_id uuid references api_keys(id) on delete set null,
  machine_id uuid references machines(id) on delete set null,
  path text not null, -- "chat/completions" | "completions" | "embeddings" | "responses" | "models" | "messages"
  model text,          -- model efetivo servido (rewrite_model / requested_model)
  status_code integer not null,
  stream boolean not null default false,
  tokens_in integer,
  tokens_out integer,
  duration_ms integer,
  created_at timestamptz not null default now()
);

create index if not exists gateway_requests_stack_idx on gateway_requests(stack_id, created_at desc);
create index if not exists gateway_requests_key_idx on gateway_requests(api_key_id, created_at desc);
create index if not exists gateway_requests_account_idx on gateway_requests(account_id, created_at desc);

alter table gateway_requests enable row level security;
