-- Acesso a modelos por plano (01/10/2026).
--
-- Até aqui qualquer chave alcançava qualquer modelo ativo da página Modelos.
-- Passa a valer o que o site do TryStac já anuncia:
--
--   Go   Qwen 3.5 9B, GLM 5.3 Flash
--   Pro  tudo do Go + Qwen 3.8 27B
--
-- 1. `plans`: planos (stacks.plan) com acesso ao modelo. Pedido de um modelo
--    da lista fora do plano da chave leva 403 no gateway, com ou sem máquina —
--    a máquina responde qualquer nome pedido (0073), então sem a trava um
--    cliente Go que pedisse o Qwen 3.8 27B receberia o 9B com o nome do 27B.
--
-- 2. `fallback_plans`: de quais planos o modelo é o RESERVA de texto (0073).
--    O reserva vira por plano porque o único que existia, o Qwen 3.8 27B, não
--    é do Go. Cada plano fica com o modelo que a própria máquina dele serve:
--    Go → Qwen 3.5 9B, Pro → Qwen 3.8 27B. O painel garante um reserva por
--    plano; o check abaixo garante que o reserva é um modelo do plano.
--
-- 3. `fallback` (0073) fica obsoleta: o gateway novo só a lê em linha sem
--    `fallback_plans`. Não é apagada aqui para o gateway antigo continuar com
--    reserva entre esta migration e o deploy dele. Remover depois.
--
-- Max e Enterprise entram em tudo (são "tudo do Pro +" no site). Modelos de
-- imagem continuam abertos a todos os planos: a decisão foi só sobre texto.
--
-- 4. Modelo na lista SEM plano nenhum = bloqueado para todos (403 "ainda não
--    está disponível"). É como o Kimi K3 entra aqui: ainda não vai ser
--    disponibilizado (decisão de 01/10), e fora da lista ele seria respondido
--    pela máquina com o nome do Kimi. Lançar = marcar os planos em /modelos.
--    Atenção: desligar o "Ativo" dele tira o bloqueio (o gateway só lê os
--    ativos).
--
-- O teto de CONTEXTO por plano (Go 32K, Pro 256K) não mora no banco: é
-- constante do gateway (plan_limits.py), como o rate limit por plano.
--
-- Ordem de deploy: esta migration → gateway → painel. Gateway novo sem esta
-- migration trata linha sem `plans` como aberta a todos (o comportamento de
-- antes), então a ordem inversa não derruba nada — só adia a trava.
--
-- Idempotente: o backfill só roda na primeira vez, quando as colunas nascem,
-- para não reabrir um modelo que o painel tenha restringido depois.

begin;

do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'openrouter_models'
      and column_name = 'plans'
  ) then
    alter table public.openrouter_models
      add column plans text[] not null default '{}',
      add column fallback_plans text[] not null default '{}';

    update public.openrouter_models
      set plans = '{Go,Pro,Max,Enterprise}', updated_at = now();

    update public.openrouter_models
      set plans = '{Pro,Max,Enterprise}', updated_at = now()
      where slug = 'qwen/qwen3.8-27b';

    update public.openrouter_models
      set fallback_plans = '{Pro,Max,Enterprise}'
      where slug = 'qwen/qwen3.8-27b';

    update public.openrouter_models
      set fallback_plans = '{Go}'
      where slug = 'qwen/qwen3.5-9b';

    alter table public.openrouter_models
      add constraint openrouter_models_fallback_in_plans
      check (fallback_plans <@ plans);
  end if;
end $$;

-- depois do backfill, de propósito: nasce sem plano (bloqueado). `do nothing`
-- para não desfazer um lançamento feito pelo painel numa reexecução.
insert into public.openrouter_models (slug, kind, label, enabled, plans, fallback_plans)
values ('moonshotai/kimi-k3', 'text', 'Kimi K3', true, '{}', '{}')
on conflict (slug) do nothing;

comment on column public.openrouter_models.plans is
  'Planos (stacks.plan) com acesso ao modelo. Fora deles o gateway responde 403 (0074)';
comment on column public.openrouter_models.fallback_plans is
  'Planos dos quais o modelo é o reserva de texto; subconjunto de plans (0074)';
comment on column public.openrouter_models.fallback is
  'OBSOLETA desde a 0074 (substituída por fallback_plans). Mantida só para o gateway antigo durante o deploy';

commit;
