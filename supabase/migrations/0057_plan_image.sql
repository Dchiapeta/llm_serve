-- Adiciona o plano "Image" (geração de imagem) aos CHECKs de plano.
--
-- O nome do plano é string literal em `templates.plan` e `stacks.plan` (text +
-- CHECK, não há enum Postgres). Mesma coreografia da 0049: o CHECK atual
-- rejeita 'Image', então precisa cair ANTES e voltar depois.
--
-- ---------------------------------------------------------------------------
-- Por que um plano NOVO, e não pendurar o template num plano existente
-- ---------------------------------------------------------------------------
-- `getDefaultTemplateForPlan` (lib/actions.ts) assume "1 template ativo por
-- plano" e desempata pelo `created_at` mais antigo. Um template de imagem
-- dentro do plano 'Max' concorreria com o template de LLM do mesmo plano: quem
-- pedisse uma stack Max poderia receber a máquina de difusão, que não fala
-- /v1/chat/completions. O plano separado é o que mantém essa resolução correta.
--
-- ---------------------------------------------------------------------------
-- Segurança de ordem de deploy
-- ---------------------------------------------------------------------------
-- Diferente da 0049, esta migration NÃO altera dado nenhum: só amplia o
-- domínio dos CHECKs. Não há janela de inconsistência, e ela pode rodar antes
-- ou depois do deploy do painel.
--
-- O gateway não precisa de deploy para esta migration. Os mapas por plano dele
-- (RATE_LIMIT_RPM, MAX_CLIENTS_BY_PLAN, MAX_DOCUMENT_BYTES, ...) são todos
-- lidos com .get(plan, DEFAULT), então um plano desconhecido cai no default em
-- vez de levantar KeyError. MAS: nesta fatia nenhuma STACK recebe plan='Image'
-- — só o template. Antes de existir a primeira stack de imagem, os espelhos
-- Python precisam ganhar a chave 'Image', senão os limites aplicados serão os
-- defaults conservadores e o painel mostrará números diferentes dos que o
-- gateway aplica.

-- 1. derruba os CHECKs. Os dois têm nome explícito desde a 0049, mas o
--    `if exists` cobre um banco onde a 0049 não rodou (o de stacks nasceu
--    anônimo na 0012).
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

-- 2. recria com 'Image' no domínio. Os defaults ('Go' nas duas tabelas, desde
--    a 0049) NÃO mudam: 'Image' é um plano que só se atribui explicitamente.
alter table templates
  add constraint templates_plan_valid
  check (plan in ('Go', 'Pro', 'Max', 'Enterprise', 'Image'));

alter table stacks
  add constraint stacks_plan_valid
  check (plan in ('Go', 'Pro', 'Max', 'Enterprise', 'Image'));
