-- Teto manual de usuários (chaves ativas) por template/máquina.
-- Null = sem teto manual; capacidade derivada apenas da VRAM.
alter table templates add column if not exists max_users integer;
alter table machines add column if not exists max_users integer;

alter table templates
  add constraint templates_max_users_positive check (max_users is null or max_users > 0);
alter table machines
  add constraint machines_max_users_positive check (max_users is null or max_users > 0);
