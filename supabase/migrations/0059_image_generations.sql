-- Registro das imagens geradas pelo plano Image: quem gerou, com que parâmetros
-- e onde o arquivo está no bucket "images" (migration 0058).
--
-- Antes desta migration o pod de difusão devolvia b64_json e nada mais: a
-- imagem existia no corpo da resposta e desaparecia. Não havia como responder
-- "quem gerou esta imagem" nem exibi-la depois.
--
-- ---------------------------------------------------------------------------
-- Não é o gateway_requests
-- ---------------------------------------------------------------------------
-- As duas tabelas registram a mesma requisição e não se substituem.
-- gateway_requests é o log de TRÁFEGO (status, duração, user-agent), escrito
-- fire-and-forget para toda rota; esta é o registro do ARTEFATO, escrito no
-- caminho crítico e só quando existe arquivo no bucket. Uma linha aqui é a
-- promessa de que o objeto em `storage_path` existe — é isso que justifica a
-- escrita ser síncrona no gateway.
--
-- ---------------------------------------------------------------------------
-- Retenção: o arquivo expira, o registro não
-- ---------------------------------------------------------------------------
-- `expires_at` é gravado pelo gateway (created_at + IMAGE_RETENTION_DAYS, 30
-- por default) em vez de calculado na leitura: retenção por plano é uma mudança
-- de política provável, e um valor por linha deixa o que já foi gravado
-- intacto. O reaper (docker/gateway/image_retention.py) apaga o ARQUIVO e o
-- PROMPT, e marca `file_deleted_at` — o resto da linha (quem, quando, qual
-- stack, que tamanho) sobrevive indefinidamente, que é o que responde à
-- pergunta de auditoria depois que a imagem já saiu.
--
-- O prompt sai junto com o arquivo por minimização: é texto livre do cliente,
-- pode conter qualquer coisa, e sem a imagem ele não serve mais para reproduzir
-- nada. Manter o prompt para sempre seria uma decisão de retenção de dado
-- pessoal tomada por omissão.
--
-- ---------------------------------------------------------------------------
-- FKs nullable
-- ---------------------------------------------------------------------------
-- `on delete set null` em todas, mesmo padrão de gateway_requests (0038): o
-- registro sobrevive à conta/stack/chave/máquina sendo removida depois. O que
-- se perde é a ligação, não a linha.

create table if not exists image_generations (
  id uuid primary key default gen_random_uuid(),

  -- Uma requisição com n>1 vira n linhas com o mesmo batch_id. Hoje
  -- IMAGE_IMAGES_PER_REQUEST_MAX é 1 (docker/image/server.py), mas é env: o
  -- agrupamento precisa existir antes de alguém subir esse número, senão as
  -- imagens de uma mesma geração ficam sem nada que as relacione.
  batch_id uuid not null,
  image_index integer not null default 0,

  account_id uuid references accounts(id) on delete set null,
  stack_id   uuid references stacks(id)   on delete set null,
  api_key_id uuid references api_keys(id) on delete set null,
  machine_id uuid references machines(id) on delete set null,

  path text not null,  -- 'images/generations' | 'images/edits'
  model text,

  -- Apagado pelo reaper junto com o arquivo (ver acima).
  prompt text,

  width integer,
  height integer,
  steps integer,
  guidance_scale numeric,

  -- numeric(20,0), e NÃO bigint. A seed do pipeline de difusão é um inteiro de
  -- 64 bits SEM sinal; o int8 do Postgres é COM sinal e para em 2^63-1. Metade
  -- do espaço de seeds estouraria o insert — e estouraria no caminho crítico,
  -- depois de a GPU já ter gerado a imagem. O CHECK documenta a faixa real em
  -- vez de deixá-la implícita na precisão.
  seed numeric(20,0)
    check (seed is null or (seed >= 0 and seed <= 18446744073709551615)),

  -- Relativo ao bucket: '{stack_id}/{yyyy-mm-dd}/{batch_id}-{i}.{ext}'. Sem
  -- prefixo "images/" — o bucket já se chama images, e repetir o nome dentro
  -- dele criaria uma pasta "images" em cada path.
  --
  -- Prefixo por STACK (não por conta) é a convenção do bucket "knowledge", pelo
  -- mesmo motivo: duas stacks da mesma conta não podem se sobrescrever.
  --
  -- O UNIQUE não é só integridade — é o que torna o insert IDEMPOTENTE. O
  -- gateway insere com `Prefer: resolution=ignore-duplicates`, então repetir o
  -- insert de um batch (retry depois de um timeout cuja resposta se perdeu no
  -- retorno) não duplica linha nem estoura 409.
  storage_path text not null unique,

  content_type text not null default 'image/png',
  bytes integer,

  created_at timestamptz not null default now(),
  expires_at timestamptz not null,

  -- Não-nulo = arquivo e prompt já removidos. É o que tira a linha da fila do
  -- reaper, e o que a UI usa para mostrar "expirada" em vez de um link quebrado.
  file_deleted_at timestamptz
);

create index if not exists image_generations_stack_idx
  on image_generations(stack_id, created_at desc);
create index if not exists image_generations_account_idx
  on image_generations(account_id, created_at desc);
create index if not exists image_generations_key_idx
  on image_generations(api_key_id, created_at desc);

-- Agrupamento de um batch na leitura (a galeria mostra as n imagens juntas).
create index if not exists image_generations_batch_idx
  on image_generations(batch_id);

-- Índice do reaper. PARCIAL de propósito: ele só procura arquivo vivo, e a
-- tabela tende a acumular linhas já expiradas para sempre (o registro fica).
-- Sem o `where`, o índice cresceria sem limite carregando justamente as linhas
-- que nunca mais serão lidas por ele.
create index if not exists image_generations_expiry_idx
  on image_generations(expires_at)
  where file_deleted_at is null;

-- RLS ligada sem policy: o gateway e o painel acessam com a service role, que
-- ignora RLS, e isso bloqueia acesso anônimo. As policies de leitura para o app
-- do cliente (image_generations_select_own, join stack_id -> stacks ->
-- accounts.user_id = auth.uid()) nascem no repo TryStac, que divide este mesmo
-- projeto Supabase — ver supabase/SHARED_SCHEMA.md.
alter table image_generations enable row level security;
