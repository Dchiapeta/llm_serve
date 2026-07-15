-- Sequência atômica para nomes de máquina (llm-stack-N). Antes, o número era
-- calculado em JS lendo o maior nome existente (nextStackMachineName em
-- lib/actions.ts) — sem lock, duas máquinas provisionadas quase ao mesmo
-- tempo (ex.: duas reservas criadas na mesma rodada do watermark proativo)
-- liam o mesmo máximo e nasciam com o mesmo nome. nextval() é atômico no
-- Postgres, elimina a corrida.
do $$
declare
  next_val bigint;
begin
  select coalesce(max((regexp_match(name, '^llm-stack-(\d+)$'))[1]::bigint), 0) + 1
    into next_val
    from machines;
  execute format('create sequence stack_machine_name_seq start with %s', next_val);
end $$;

create or replace function next_stack_machine_name()
returns text
language sql
security definer
set search_path = public
as $$
  select 'llm-stack-' || nextval('stack_machine_name_seq')::text
$$;

-- Postgres concede EXECUTE a PUBLIC em função nova por padrão — com
-- security definer isso deixaria qualquer chamador com a anon key avançar
-- a sequence sem autenticação (mesmo padrão pré-existente de claim_route/
-- touch_route em 0005, mas fechamos aqui já que é barato).
revoke execute on function next_stack_machine_name() from public, anon, authenticated;
grant execute on function next_stack_machine_name() to service_role;

-- Trava contra nome duplicado também no caso que a sequence sozinha não
-- cobre: uma máquina criada manualmente pelo painel (createMachine aceita
-- nome livre) usando o prefixo llm-stack- e colidindo, no futuro, com um
-- valor que a sequence ainda vai emitir. Escopo parcial (só não-terminadas)
-- porque já existem nomes duplicados históricos em máquinas terminated
-- (da própria corrida que este arquivo corrige) — não vale a pena migrá-los.
create unique index if not exists machines_name_active_key
  on machines (name)
  where status <> 'terminated';
