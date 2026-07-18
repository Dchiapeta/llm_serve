-- A migration 0020 fechou a MAIORIA do vazamento de system_prompt/RAG entre
-- stacks da mesma conta, mas deixou duas caudas documentadas como "legado
-- compartilhado até re-indexar": (a) chunks com stack_id NULL retornados
-- pela RPC pra QUALQUER stack da conta; (b) chave sem stack_id cai no
-- heurístico pick_stack, que pode escolher a stack errada entre as da
-- mesma conta. Enquanto o produto era só teste interno isso era aceitável;
-- indo pra produção real com clientes de verdade, essas duas caudas viram
-- vazamento cross-tenant de fato. Esta migration fecha as duas.

-- 1) Backfill: toda api_key ATIVA sem stack_id passa a ter o MESMO stack
-- que resolve_key_stack já escolhe hoje em runtime via pick_stack (stack
-- mais recente do mesmo plano da conta; sem stack do plano, a mais recente
-- de qualquer plano) — não muda nenhum comportamento observável hoje, só
-- torna permanente uma escolha que já era feita a cada request. Sem isso,
-- pick_stack podia MUDAR de escolha no futuro (sempre pega "a mais
-- recente") se uma stack nova do mesmo plano fosse criada na mesma conta
-- depois da chave já emitida — a chave passaria a servir outra stack sem
-- nenhuma mudança na própria chave.
with picked as (
  select distinct on (s.account_id)
    s.account_id,
    s.id as stack_id
  from stacks s
  order by
    s.account_id,
    -- mesmo critério de pick_stack: mesmo plano da conta primeiro, depois
    -- mais recente (dentro do pool de mesmo plano, ou de todas se nenhuma
    -- stack bate com o plano da conta)
    (s.plan = (select plan from accounts a where a.id = s.account_id)) desc,
    s.created_at desc
)
update api_keys
set stack_id = picked.stack_id
from picked
where api_keys.account_id = picked.account_id
  and api_keys.status = 'active'
  and api_keys.stack_id is null;

-- 2) RPC endurecida: remove os dois fallbacks de 0020 que devolviam dado de
-- fora da stack exata (stack_id is null -> chunk legado de qualquer stack
-- da conta; p_stack_id is null -> toda a conta). Com o backfill acima, toda
-- chave ativa de conta com stack manda um p_stack_id real. Chunk sem
-- stack_id que sobrar (conta que tinha 2+ stacks na época da 0020 e nunca
-- foi re-indexada) fica órfão — inacessível via RAG até um admin reindexar
-- pra stack certa — mas nunca mais vaza pra stack errada: não existe
-- heurístico seguro pra adivinhar a stack certa desses chunks legados só a
-- partir do SQL, então o lado seguro é deixar de servir em vez de arriscar
-- servir errado.
drop function if exists match_knowledge_chunks(uuid, uuid, vector, int);

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
    and stack_id = p_stack_id
  order by embedding <=> p_query_embedding
  limit p_top_k
$$;
