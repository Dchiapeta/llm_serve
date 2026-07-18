-- lora_adapters era indexado só por account_id (0004_lora_adapters.sql),
-- mas uma conta pode ter várias stacks — o fine-tune, assim como
-- system_prompt e RAG antes dele (0020), tem que pertencer à STACK, não à
-- conta inteira. storage_path e o nome do adapter no vLLM (acct-{accountId},
-- ver lib/actions.ts loraName) continuam iguais — são só convenção física de
-- armazenamento, não a fonte de verdade de roteamento; renomear exigiria
-- mover arquivos reais no bucket, fora de escopo aqui.
alter table lora_adapters
  add column if not exists stack_id uuid references stacks(id) on delete cascade;

-- Backfill sem ambiguidade: conta com exatamente 1 stack, associa direto.
-- Conta com 2+ stacks fica com stack_id null até um admin reassociar
-- manualmente pelo painel — mesmo padrão já usado em 0020 pra
-- knowledge_chunks ambíguos (órfão até reindexar, nunca vaza pra stack errada).
update lora_adapters la
set stack_id = s.id
from stacks s
where la.stack_id is null
  and s.account_id = la.account_id
  and (select count(*) from stacks s2 where s2.account_id = la.account_id) = 1;

create index if not exists lora_adapters_stack_idx on lora_adapters(stack_id);
