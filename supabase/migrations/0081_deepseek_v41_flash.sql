-- DeepSeek V4.1 Flash no Pro (30/10/2026).
--
-- Entra na allowlist do repasse (0071) liberado para Pro e para quem é "tudo
-- do Pro +" (Max, Enterprise), mesmo critério do Qwen 3.8 27B na 0074. Não é
-- reserva de nenhum plano: `fallback_plans` fica vazio.
--
-- O site do TryStac (LP, preços e docs/models) já anuncia o modelo no Pro.
--
-- Idempotente: `do nothing` para não desfazer o que o painel /modelos tenha
-- mudado depois (planos, rótulo, ativo).

insert into public.openrouter_models (slug, kind, label, enabled, plans, fallback_plans)
values (
  'deepseek/deepseek-v4.1-flash-1017',
  'text',
  'DeepSeek V4.1 Flash',
  true,
  '{Pro,Max,Enterprise}',
  '{}'
)
on conflict (slug) do nothing;
