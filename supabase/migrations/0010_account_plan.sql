-- Plano de produto da conta (VibeCoder/Pro/Max/Enterprise) e system prompt
-- configurável. Sem isso o roteamento não sabe a qual template/modelo uma
-- conta sem adapter LoRA deve ser presa (ver list_running_machines_for_plan).
alter table accounts
  add column if not exists plan text not null default 'VibeCoder';

alter table accounts
  add constraint accounts_plan_valid
  check (plan in ('VibeCoder', 'Pro', 'Max', 'Enterprise'));

-- Override de system prompt por conta; null = sem override (VibeCoder usa
-- sempre um, mas a coluna é genérica pros outros planos também).
alter table accounts
  add column if not exists system_prompt text;
