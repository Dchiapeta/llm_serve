-- Atividade por máquina: tocada pelo gateway a cada request proxied (throttle
-- de 15s). Base da auto-pausa: máquina running sem atividade há
-- MACHINE_IDLE_STOP_MINUTES e sem rotas ativas é pausada (stopPod).
-- Default now() dá carência a máquinas recém-criadas.
alter table machines
  add column if not exists last_activity_at timestamptz not null default now();
