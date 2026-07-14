-- Base de conhecimento (RAG básico) do VibeCoder: chunks de texto enviados
-- pela conta, com embedding gerado por API externa (OpenAI text-embedding-3-small,
-- 1536 dimensões). Arquivos crus ficam no bucket "knowledge" do Storage
-- (prefixo {account_id}/{filename}), espelhando o padrão do bucket "loras".
create extension if not exists vector;

create table if not exists knowledge_chunks (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  storage_path text not null,
  chunk_index int not null,
  content text not null,
  embedding vector(1536) not null,
  created_at timestamptz not null default now()
);

create index if not exists knowledge_chunks_account_idx on knowledge_chunks(account_id);
create index if not exists knowledge_chunks_storage_path_idx on knowledge_chunks(storage_path);

alter table knowledge_chunks enable row level security;

-- RPC compartilhada (mesmo padrão de machine_lora_slots): top-k por
-- similaridade de cosseno, escopado à conta — chamada pelo gateway.
create or replace function match_knowledge_chunks(
  p_account_id uuid,
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
  order by embedding <=> p_query_embedding
  limit p_top_k
$$;
