-- Histórico de estado das máquinas — base da aba Financeiro (gasto real vs.
-- custo hipotético 24/7, ou seja, quanto o auto-pause economiza).
--
-- Motivação: o painel só sabia o custo INSTANTÂNEO (soma de cost_per_hr das
-- máquinas running agora). Para saber quanto gastamos de fato num período é
-- preciso saber quanto tempo cada máquina ficou LIGADA — e isso não existia
-- em lugar nenhum.
--
-- Por que não derivar de machine_events: o reconciler do RunPod muda
-- machines.status SEM gravar evento (docker/gateway/lifecycle.py
-- reconcile_statuses_once e lib/machines.ts reconcileMachineStatuses). Uma
-- máquina pausada/religada pelo console do RunPod sumiria do histórico.
-- Um trigger na própria tabela machines captura TODOS os escritores (painel,
-- gateway, reconcilers) sem tocar em código de aplicação.

create table if not exists machine_runtime_intervals (
  id uuid primary key default gen_random_uuid(),
  machine_id uuid not null references machines(id) on delete cascade,
  status text not null, -- creating | running | stopped | terminated | error
  -- congelado na abertura do intervalo: o preço da GPU no RunPod pode mudar,
  -- e o gasto passado tem que continuar valendo o que valia na época
  cost_per_hr numeric,
  started_at timestamptz not null default now(),
  ended_at timestamptz -- null = intervalo vigente
);

create index if not exists mri_machine_window_idx
  on machine_runtime_intervals (machine_id, started_at desc);
-- o trigger fecha o intervalo vigente a cada transição; este índice é o
-- caminho quente dele
create index if not exists mri_open_idx
  on machine_runtime_intervals (machine_id) where ended_at is null;
create index if not exists mri_window_idx
  on machine_runtime_intervals (started_at desc);

alter table machine_runtime_intervals enable row level security;

-- Fecha o intervalo vigente da máquina e abre outro com o estado novo. Os
-- intervalos de uma máquina são contíguos desde a criação dela, então a soma
-- das durações dentro de uma janela é o "tempo de vida na janela" — é isso
-- que dá o baseline 24/7 sem inflar máquinas recém-criadas.
create or replace function track_machine_runtime() returns trigger
language plpgsql as $$
begin
  update machine_runtime_intervals
     set ended_at = now()
   where machine_id = new.id and ended_at is null;

  insert into machine_runtime_intervals (machine_id, status, cost_per_hr)
  values (new.id, new.status, new.cost_per_hr);

  return new;
end $$;

drop trigger if exists machines_runtime_insert on machines;
create trigger machines_runtime_insert
  after insert on machines
  for each row execute function track_machine_runtime();

-- O WHEN não é cosmético: o gateway toca machines.last_activity_at a cada
-- request (throttle de 15s, supa.py touch_machine_activity) e o reconciler do
-- painel reescreve status/cost_per_hr em lote. Sem o "is distinct from",
-- qualquer UPDATE que mencione as colunas geraria um intervalo de duração
-- zero — a tabela viraria lixo em minutos.
drop trigger if exists machines_runtime_update on machines;
create trigger machines_runtime_update
  after update of status, cost_per_hr on machines
  for each row
  when (old.status is distinct from new.status
        or old.cost_per_hr is distinct from new.cost_per_hr)
  execute function track_machine_runtime();

-- Backfill: só o presente. O passado não é reconstruível com confiança (ver
-- nota sobre machine_events acima), então cada máquina viva ganha um intervalo
-- vigente começando agora. O histórico financeiro nasce vazio e engorda a
-- partir daqui. Idempotente: não duplica se a migration rodar duas vezes.
insert into machine_runtime_intervals (machine_id, status, cost_per_hr, started_at)
select id, status, cost_per_hr, now()
from machines m
where m.status <> 'terminated'
  and not exists (
    select 1 from machine_runtime_intervals mri
    where mri.machine_id = m.id and mri.ended_at is null
  );
