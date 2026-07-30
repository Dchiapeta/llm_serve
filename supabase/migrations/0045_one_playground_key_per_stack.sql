-- Garante no máximo 1 chave de Playground ativa por stack no nível do banco
-- (não só por lógica de aplicação) — é a "global_api_key" interna, nunca
-- exposta/criável pelo cliente. Sem isso, duas chamadas concorrentes a
-- getOrCreatePlaygroundKey (lib/actions.ts) poderiam nascer com 2 chaves
-- ativas pra mesma stack (check-then-insert sem trava).
create unique index if not exists api_keys_one_active_playground_per_stack
  on api_keys (stack_id)
  where purpose = 'playground' and status = 'active';
