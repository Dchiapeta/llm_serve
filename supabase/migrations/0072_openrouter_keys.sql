-- Uma chave do OpenRouter para cada chave da Stac (29/09/2026).
--
-- O repasse ao OpenRouter (0071) usava uma chave compartilhada. Para ver o
-- custo POR CHAVE no próprio OpenRouter (a página Activity agrupa por API key),
-- cada chave da Stac ganha uma chave espelho lá, criada pela API de
-- gerenciamento do OpenRouter. Sem teto de gasto por enquanto: só
-- acompanhamento. Ver docker/gateway/openrouter_keys.py.
--
-- O segredo da chave espelho só é devolvido uma vez, na criação, e o gateway
-- precisa dele em texto puro para chamar o OpenRouter — por isso vai CIFRADO
-- (Fernet, chave na env OPENROUTER_KEYS_ENCRYPTION_KEY do gateway), não em hash.
--
-- Tabela própria, e não uma coluna de api_keys, de propósito: o cliente do
-- TryStac lê a própria api_keys via RLS, e este projeto Supabase é
-- compartilhado com ele. Aqui é só service role.
--
-- Idempotente.

begin;

create table if not exists public.openrouter_keys (
  api_key_id uuid primary key references public.api_keys(id) on delete cascade,
  -- identificador da chave no OpenRouter (data.hash), usado pela API de
  -- gerenciamento (GET/PATCH/DELETE /api/v1/keys/{hash})
  openrouter_hash text not null unique,
  secret_encrypted text not null,
  -- nome com que ela aparece no OpenRouter (conta · prefixo · id)
  name text not null,
  created_at timestamptz not null default now()
);

-- RLS ligada sem policy + sem grant para anon/authenticated: só service role.
-- O revoke é explícito porque o Supabase concede select a anon/authenticated
-- por default em objetos novos do schema public (ver 0067).
alter table public.openrouter_keys enable row level security;
revoke all on public.openrouter_keys from public, anon, authenticated;

commit;
