-- Renomeia o plano "VibeCoder" para "Go".
--
-- O nome do plano é string literal em `templates.plan` e `stacks.plan` (text +
-- CHECK, não há enum Postgres). Ordem obrigatória: o CHECK antigo rejeita 'Go',
-- então precisa cair ANTES do update e voltar depois.
--
-- Pré-requisito: o gateway já precisa estar deployado aceitando os dois nomes
-- (SHARED_POD_PLANS/REASONING_LEAK_PLANS em docker/gateway/main.py). Sem isso,
-- na janela entre esta migration e o deploy o plano sai dos sets e o filtro de
-- <think> desliga.
--
-- NÃO renomeia o alias servido pelo vLLM (--served-model-name vibecoder-base):
-- esse valor só muda com recriação do pod, e machines.served_model_name tem que
-- continuar refletindo o que o pod REALMENTE serve, senão o pin_model do gateway
-- devolve 404 em toda request. Ver etapa 5 do plano.

-- 1. derruba os CHECKs. O de stacks foi criado inline em 0012 (sem nome
--    explícito), então o nome é gerado pelo Postgres — descobre pelo catálogo
--    em vez de chutar 'stacks_plan_check'.
alter table templates drop constraint if exists templates_plan_valid;

do $$
declare
  c text;
begin
  for c in
    select con.conname
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_attribute att on att.attrelid = rel.oid and att.attnum = any (con.conkey)
    where rel.relname = 'stacks'
      and con.contype = 'c'
      and att.attname = 'plan'
  loop
    execute format('alter table stacks drop constraint %I', c);
  end loop;
end $$;

-- 2. renomeia os valores existentes
update templates set plan = 'Go' where plan = 'VibeCoder';
update stacks    set plan = 'Go' where plan = 'VibeCoder';

-- 3. recria os CHECKs com o nome novo
alter table templates
  add constraint templates_plan_valid
  check (plan in ('Go', 'Pro', 'Max', 'Enterprise'));

alter table stacks
  add constraint stacks_plan_valid
  check (plan in ('Go', 'Pro', 'Max', 'Enterprise'));

-- 4. defaults (0009 e 0012 apontavam para 'VibeCoder', que agora é inválido —
--    sem isto qualquer insert sem plan explícito viola o CHECK novo)
alter table templates alter column plan set default 'Go';
alter table stacks    alter column plan set default 'Go';

-- 5. nome do template no painel (cosmético; o template no RunPod é renomeado
--    pelo updateTemplate quando o registro for salvo pela UI)
update templates
set name = 'Go_A40_Qwen3.5'
where name = 'VibeCoder_A40_Qwen3.5';
