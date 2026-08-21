-- Overrides de sampling POR CHAVE: temperature, top_p, max_tokens e
-- presence_penalty deixam de ser só um clamp de segurança/default de STACK
-- igual pra todo mundo (main.py) e passam a poder ser fixados por
-- credencial — mesmo desenho do system prompt por chave (0053): a variação
-- mora na credencial, não no corpo da request nem só na stack.
--
-- Sem switch (diferente de use_custom_prompt/system_prompt): aqui NULL já
-- significa "sem override" sem ambiguidade, porque não existe rascunho a
-- preservar — não há "desligar e religar depois" pra um número, então uma
-- coluna por parâmetro basta.
--
-- Precedência (docker/gateway/main.py, apply_key_sampling_defaults): valor
-- da CHAVE, se não nulo, ganha do default da STACK (default_temperature/
-- default_top_p, 0035 — que continua existindo pra contas que preferem
-- configurar no nível da stack) e do clamp de segurança global
-- (MAX_MAX_TOKENS etc.). Só quando nem chave nem stack têm valor é que o
-- comportamento atual (passthrough do cliente + clamp) prevalece.
--
-- Os CHECKs espelham o clamp do gateway (main.py, validate_body) pelo mesmo
-- motivo do 0035: um valor fora da faixa nunca deveria ter sido gravado, e
-- rejeitar aqui pega erro de quem escreve nestas colunas (o painel do
-- cliente, fora deste repo) antes dele virar tráfego real.
--
-- ATENÇÃO ordem de deploy (mesmo aviso do 0053): find_active_key (docker/
-- gateway/supa.py) passa a pedir estas 4 colunas no select, e coluna
-- inexistente vira PostgREST 400 dentro de um raise_for_status — 500 em
-- 100% do tráfego. Esta migration tem que estar aplicada ANTES do deploy do
-- gateway.
alter table api_keys
  add column if not exists default_temperature numeric
    check (default_temperature is null or (default_temperature >= 0 and default_temperature <= 2)),
  add column if not exists default_top_p numeric
    check (default_top_p is null or (default_top_p >= 0 and default_top_p <= 1)),
  add column if not exists default_max_tokens integer
    check (default_max_tokens is null or default_max_tokens > 0),
  add column if not exists default_presence_penalty numeric
    check (default_presence_penalty is null or (default_presence_penalty >= -2 and default_presence_penalty <= 2));
