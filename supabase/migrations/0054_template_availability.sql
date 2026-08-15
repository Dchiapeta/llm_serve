-- Disponibilidade operacional dos produtos/templates.
--
-- is_enabled=false bloqueia qualquer criação de máquina e qualquer NOVA
-- alocação de usuário. Máquinas e stacks já existentes não são desligadas nem
-- desvinculadas pela migration.
--
-- is_test=true mantém a criação MANUAL de máquinas disponível para o admin,
-- mas exclui o template do provisionamento automático e do roteamento de
-- novos usuários. Defaults preservam exatamente o comportamento anterior.
alter table templates
  add column if not exists is_enabled boolean not null default true,
  add column if not exists is_test boolean not null default false;

comment on column templates.is_enabled is
  'False bloqueia novas máquinas e novas alocações; recursos existentes são preservados.';

comment on column templates.is_test is
  'True permite máquina manual, mas bloqueia provisionamento automático e novas alocações.';
