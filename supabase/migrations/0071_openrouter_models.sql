-- Repasse de inferência para o OpenRouter (28/09/2026).
--
-- Por um período a Stac deixa de subir máquinas próprias para os modelos que o
-- cliente pedir e repassa a request ao OpenRouter. O cliente escolhe o modelo
-- pelo campo `model` da request; quais modelos são aceitos é decidido na página
-- "Modelos (OpenRouter)" do painel. Ver docker/gateway/openrouter.py.
--
-- 1. Dois interruptores globais em system_settings (lidos pelo gateway com o
--    mesmo cache curto do auto_provision_enabled, 0016):
--      machines_enabled    máquinas próprias (RunPod). Desligado: nenhuma
--                          request vai para máquina e nada liga GPU sozinho
--                          (wake, provisionamento e recriação são negados).
--                          Nasce LIGADO: é o comportamento de antes desta
--                          migration.
--      openrouter_enabled  o repasse. Nasce desligado.
--
-- 2. openrouter_models: a allowlist. `slug` é o id do modelo no OpenRouter
--    (ex.: anthropic/claude-sonnet-4.5), que é também o que o cliente manda em
--    `model`. `kind` separa texto de imagem: uma chave de stack de texto só
--    alcança modelos de texto, uma de imagem só os de imagem.
--
-- 3. gateway_requests ganha `upstream` (NULL = máquina, 'openrouter' = repasse)
--    e `cost_usd`, o custo que o OpenRouter devolve em cada resposta. É o que a
--    Stac paga — o gateway tira esse campo do corpo antes de entregar ao
--    cliente. machine_id já era nullable (0038).
--
-- Ordem de deploy: esta migration → gateway → painel. O gateway só grava as
-- colunas novas nas linhas repassadas, então um gateway novo sem esta migration
-- não perde o log das máquinas.
--
-- Idempotente.

begin;

insert into public.system_settings (key, value) values
  ('machines_enabled', true),
  ('openrouter_enabled', false)
on conflict (key) do nothing;

create table if not exists public.openrouter_models (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  kind text not null check (kind in ('text', 'image')),
  label text,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- RLS ligada sem policy: só service role (painel e gateway), mesmo padrão de
-- system_settings.
alter table public.openrouter_models enable row level security;

alter table public.gateway_requests
  add column if not exists upstream text,
  add column if not exists cost_usd numeric(14, 8);

comment on column public.gateway_requests.upstream is
  'NULL = servida por máquina da Stac; ''openrouter'' = repassada ao OpenRouter (0071)';
comment on column public.gateway_requests.cost_usd is
  'Custo em USD cobrado pelo OpenRouter (usage.cost). NULL nas requests de máquina (0071)';

commit;
