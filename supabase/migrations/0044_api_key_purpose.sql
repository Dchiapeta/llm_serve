-- Distingue chave "customer" (visível ao cliente, conta pra slot/cota) de
-- chave "playground" (interna, gerada junto com a stack, nunca exibida ao
-- cliente, isenta de slot de capacidade e de cota diária de tokens — usada
-- pelo admin para testar o modelo do cliente sem afetar o uso dele).
alter table api_keys
  add column if not exists purpose text not null default 'customer'
  check (purpose in ('customer', 'playground'));

create index if not exists api_keys_stack_purpose_idx
  on api_keys (stack_id, purpose)
  where status = 'active';

-- account_token_usage_today soma tokens_in+tokens_out de usage_metrics do dia
-- pra aplicar a cota diária do plano (docker/gateway/main.py:check_token_quota).
-- Chave "playground" fica de fora da soma: ela não tem cota própria (o
-- gateway pula o enforcement pra ela) e não pode inflar a cota da chave
-- "customer" da mesma conta.
create or replace function account_token_usage_today(p_account_id uuid)
returns bigint language sql security definer stable as $$
  select coalesce(sum(um.tokens_in + um.tokens_out), 0)::bigint
  from usage_metrics um
  join api_keys ak on ak.id = um.api_key_id
  where ak.account_id = p_account_id
    and ak.purpose = 'customer'
    and um.window_start >= date_trunc('day', now())
$$;
