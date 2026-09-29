-- Nível de raciocínio POR CHAVE: `default_reasoning_effort`.
--
-- A 0064 deu à chave (e à stack) o liga/desliga do thinking
-- (default_enable_thinking), mas o nível — low/medium/high, que o chat
-- template do Qwen3.8 lê como reasoning_effort (thinking_policy.py,
-- EFFORT_TO_TEMPLATE) — só existia por request. O Playground do painel do
-- cliente deixa testar cada nível e agora cria a chave "com estes
-- parâmetros": sem coluna, o nível testado se perdia na hora de salvar.
--
-- Uma coluna só, com quatro valores, em vez de um segundo boolean: `none` já
-- diz "sem raciocínio" e low/medium/high dizem "com raciocínio, neste nível".
-- NULL é herdar — cai em default_enable_thinking da chave, depois da stack,
-- depois no legado — e é o que garante que nenhuma chave existente mude de
-- comportamento ao aplicar esta migration. Quando não nula, a coluna tem
-- precedência sobre default_enable_thinking da própria chave: o nível é a
-- configuração mais específica. Request explícita continua ganhando de tudo.
--
-- Só em api_keys: a stack segue com o boolean da 0064. O nível é uma escolha
-- de integração (a mesma stack serve o agente que raciocina "high" e o
-- atendimento que responde direto), não de máquina.
--
-- O CHECK espelha o vocabulário do gateway (resolve_thinking_policy): um
-- valor fora dele nunca deveria ter sido gravado, e rejeitar aqui pega o erro
-- de quem escreve (o painel do cliente, fora deste repo) antes de virar
-- tráfego recusado.
--
-- ORDEM DE DEPLOY (mesmo aviso da 0064): esta migration ANTES do gateway. O
-- supa.py passa a pedir default_reasoning_effort no select de api_keys, e sem
-- a coluna o PostgREST recusa a query inteira — o que derruba a resolução de
-- chave, ou seja, todo o tráfego.
alter table public.api_keys
  add column if not exists default_reasoning_effort text
    check (
      default_reasoning_effort is null
      or default_reasoning_effort in ('none', 'low', 'medium', 'high')
    );

comment on column public.api_keys.default_reasoning_effort is
  'NULL = herda default_enable_thinking (chave, depois stack, depois legado); none = sem raciocínio; low/medium/high = raciocínio ligado nesse nível. Request explícita tem precedência.';
