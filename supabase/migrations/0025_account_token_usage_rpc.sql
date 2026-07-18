-- RPC pra somar o uso de tokens do dia corrente de uma conta, através de
-- todas as chaves e máquinas dela — base da quota diária de tokens do
-- gateway (proteção de custo real, não só limite de requisições).
create or replace function account_token_usage_today(p_account_id uuid)
returns bigint
language sql
security definer
stable
as $$
  select coalesce(sum(um.tokens_in + um.tokens_out), 0)::bigint
  from usage_metrics um
  join api_keys ak on ak.id = um.api_key_id
  where ak.account_id = p_account_id
    and um.window_start >= date_trunc('day', now())
$$;
