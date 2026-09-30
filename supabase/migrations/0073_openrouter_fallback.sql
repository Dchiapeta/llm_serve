-- OpenRouter como RESERVA das máquinas (30/09/2026).
--
-- Até a 0071 o destino era decidido só pelo modelo pedido: modelo da lista
-- ia sempre para o OpenRouter. A regra passa a ser, com os dois interruptores
-- ligados:
--
--   * máquina do plano disponível → a máquina responde, QUALQUER que seja o
--     modelo pedido (e a resposta mostra o nome que o cliente pediu);
--   * sem máquina disponível (pausada, subindo, sendo criada ou lotada), ou
--     conversa maior que a janela da máquina → OpenRouter, enquanto o gateway
--     liga/cria a máquina do plano em paralelo;
--   * no OpenRouter responde o modelo PEDIDO se ele está na lista; senão o
--     RESERVA de texto — é o que atende `go-base`/`pro-base`, nomes que só
--     existem nas máquinas.
--
-- Com as máquinas desligadas, tudo vai para o OpenRouter pela mesma escolha
-- de modelo (pedido na lista, senão reserva).
--
-- IMAGEM fica FORA desta regra (decisão do usuário): modelo de imagem da lista
-- vai direto para o OpenRouter sem ligar/criar máquina de imagem nenhuma (já
-- era assim desde a 0071), e o resto segue para a máquina de imagem como
-- sempre. Não existe reserva de imagem.
--
-- `fallback` marca o reserva de texto (Qwen3.8 27B, decisão do usuário). A
-- coluna vale por linha e o índice garante no máximo um por tipo; o gateway
-- só consulta o de texto.
--
-- Idempotente.

begin;

alter table public.openrouter_models
  add column if not exists fallback boolean not null default false;

create unique index if not exists openrouter_models_one_fallback_per_kind
  on public.openrouter_models (kind) where fallback;

insert into public.openrouter_models (slug, kind, label, enabled, fallback)
values ('qwen/qwen3.8-27b', 'text', 'Qwen3.8 27B', true, true)
on conflict (slug) do update set fallback = true, enabled = true, updated_at = now();

commit;
