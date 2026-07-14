-- Vincula cada stack à máquina que a serve. Nullable: stacks do backfill
-- da 0012 ficam sem máquina; on delete set null preserva a stack quando a
-- máquina é apagada do banco.
alter table stacks
  add column if not exists machine_id uuid references machines(id) on delete set null;

create index if not exists stacks_machine_idx on stacks(machine_id);
