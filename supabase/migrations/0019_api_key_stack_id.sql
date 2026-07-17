-- Vincula cada chave à stack específica que a originou, permitindo o
-- gateway rotear direto pela stack (e seu plano) em vez de adivinhar pelo
-- accounts.plan — que quebra quando uma conta tem stacks de planos
-- diferentes (Stack = "uma conta pode ter várias", mas accounts.plan é um
-- valor só). Nullable + on delete set null: chaves criadas antes desta
-- migration, e o dialog de "gerar chave avulsa" do painel (sem stack no
-- contexto), continuam funcionando via o heurístico antigo por accounts.plan.
alter table api_keys
  add column if not exists stack_id uuid references stacks(id) on delete set null;

create index if not exists api_keys_stack_idx on api_keys(stack_id);
