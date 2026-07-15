-- Flags globais chave-valor, lidas tanto pelo painel quanto pelo gateway.
-- auto_provision_enabled: interruptor do provisionamento automático de
-- máquinas (cascata reativa + reposição proativa) — nasce desligado, só liga
-- quando alguém aperta o botão no painel (é uma automação que gasta GPU
-- sozinha, não deve entrar em produção já ativa).
create table system_settings (
  key text primary key,
  value boolean not null,
  updated_at timestamptz not null default now()
);

-- RLS habilitado sem policies: acesso só via service role (painel e
-- gateway), mesmo padrão de routing_state/routing_history/stacks.
alter table system_settings enable row level security;

insert into system_settings (key, value) values ('auto_provision_enabled', false);
