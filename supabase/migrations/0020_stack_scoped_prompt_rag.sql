-- System prompt e base de conhecimento (RAG) eram escopados por accounts,
-- não por stacks — herança de quando accounts.system_prompt (migration 0010)
-- e knowledge_chunks (0011) foram criadas, ANTES da tabela stacks existir
-- (0012, "uma conta pode ter várias stacks"). Resultado: uma conta com
-- múltiplas stacks compartilhava o mesmo prompt e a mesma base entre todas.
-- Esta migration move os dois para a stack, com accounts.system_prompt
-- preservada só como fallback legado (chaves anteriores à migration 0019,
-- sem stack_id resolvível).

alter table stacks
  add column if not exists system_prompt text;

-- Backfill: cada stack existente herda o prompt atual da conta — preserva o
-- comportamento de hoje no momento da migration; a partir daqui cada stack
-- diverge conforme editada individualmente pelo painel.
update stacks
set system_prompt = accounts.system_prompt
from accounts
where accounts.id = stacks.account_id
  and accounts.system_prompt is not null
  and stacks.system_prompt is null;

alter table knowledge_chunks
  add column if not exists stack_id uuid references stacks(id) on delete cascade;

create index if not exists knowledge_chunks_stack_idx on knowledge_chunks(stack_id);

-- Backfill: só o caso não-ambíguo (conta com exatamente 1 stack) resolve
-- sozinho a qual stack os chunks já indexados pertencem. Contas com 2+
-- stacks ficam com stack_id null nos chunks antigos — a RPC abaixo trata
-- isso como contexto legado compartilhado até o admin re-indexar por stack.
with single_stack_accounts as (
  -- uuid não tem min/max agregado nativo — array_agg + índice serve porque
  -- o having já garante exatamente 1 linha por grupo.
  select account_id, (array_agg(id))[1] as stack_id
  from stacks
  group by account_id
  having count(*) = 1
)
update knowledge_chunks
set stack_id = single_stack_accounts.stack_id
from single_stack_accounts
where single_stack_accounts.account_id = knowledge_chunks.account_id
  and knowledge_chunks.stack_id is null;

-- RPC atualizada: aceita p_stack_id (nullable). Com stack_id resolvido,
-- retorna os chunks da própria stack MAIS os legados sem stack_id (contexto
-- compartilhado ainda não migrado); sem stack_id (chave legada), mantém o
-- comportamento antigo de buscar por toda a conta.
-- Assinatura antiga (3 args) precisa ser dropada primeiro: "create or
-- replace" não substitui quando a lista de parâmetros muda — criaria uma
-- segunda função sobrecarregada em vez de atualizar esta.
drop function if exists match_knowledge_chunks(uuid, vector, int);

create or replace function match_knowledge_chunks(
  p_account_id uuid,
  p_stack_id uuid,
  p_query_embedding vector(1536),
  p_top_k int default 4
)
returns table(content text)
language sql
security definer
as $$
  select content
  from knowledge_chunks
  where account_id = p_account_id
    and (p_stack_id is null or stack_id = p_stack_id or stack_id is null)
  order by embedding <=> p_query_embedding
  limit p_top_k
$$;
