-- Modelos de decisão no repasse ao OpenRouter (07/10/2026).
--
-- O Jev (typesafe/jev-1.13, da TypeSafe) não gera texto nem imagem: recebe um
-- `state` e perguntas tipadas (choice/noul/score) e devolve as respostas com
-- probabilidades. No OpenRouter ele só é servido pela API de Decisions
-- (/api/alpha/decisions), nunca pelo chat/completions. O gateway ganhou a rota
-- POST /v1/decisions para isso (ver docker/gateway/openrouter.py), e
-- openrouter_models.kind passa a aceitar 'decisions'.
--
-- Um modelo 'decisions' só é alcançado pelo /v1/decisions, e o /v1/decisions
-- só alcança modelos 'decisions'. Não pode ser o reserva de texto (o painel já
-- recusa reserva fora de 'text', e o índice da 0073 é por kind).
--
-- Ordem de deploy: esta migration → gateway → painel. Painel novo sem ela
-- recebe violação do check ao cadastrar o Jev; gateway novo sem ela só não
-- encontra modelo 'decisions' na lista e responde 404.
--
-- Idempotente.

begin;

alter table public.openrouter_models
  drop constraint if exists openrouter_models_kind_check;

alter table public.openrouter_models
  add constraint openrouter_models_kind_check
  check (kind in ('text', 'image', 'decisions'));

commit;
