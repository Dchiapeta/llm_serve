-- Denormaliza stack_id em usage_metrics, gravado pelo gateway no momento do
-- insert (ele já resolve a stack da chave pra rotear cada requisição). Antes,
-- o consumo por stack só dava pra calcular via join usage_metrics -> api_keys
-- -> stacks (ver stack_usage_stats, migration 0032), que exclui silenciosamente
-- linhas de chaves órfãs (api_keys.stack_id nulo, chave avulsa sem stack no
-- contexto) e força um join extra em toda agregação de uso por stack.
--
-- Nullable + on delete set null: mesmo caso de api_keys.stack_id (0019) —
-- chaves órfãs continuam gravando uso, só que sem atribuição a nenhuma stack.
alter table usage_metrics
  add column if not exists stack_id uuid references stacks(id) on delete set null;

create index if not exists usage_metrics_stack_idx on usage_metrics(stack_id, window_start desc);

-- Backfill do histórico via o join que já existia antes desta migration.
update usage_metrics u
set stack_id = ak.stack_id
from api_keys ak
where ak.id = u.api_key_id
  and u.stack_id is null
  and ak.stack_id is not null;
