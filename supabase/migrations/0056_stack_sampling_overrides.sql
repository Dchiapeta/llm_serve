-- Overrides de sampling POR STACK: max_tokens e presence_penalty deixam de
-- existir só por CHAVE (api_keys, 0055) e passam a ter também um default no
-- nível da stack — mesmo desenho de default_temperature/default_top_p (0035),
-- que já servem esse papel pra temperature/top_p.
--
-- Precedência (docker/gateway/main.py, apply_stack_sampling_defaults, chamada
-- logo após apply_key_sampling_defaults): valor da CHAVE, se não nulo, ganha
-- do default da STACK, que ganha do passthrough/clamp de segurança global.
--
-- Os CHECKs espelham exatamente os da 0055 pra api_keys (mesmo motivo: um
-- valor fora da faixa nunca deveria ter sido gravado, e rejeitar aqui pega
-- erro de quem escreve nestas colunas — a rota PATCH /api/stacks/[id]/
-- model-config deste repo, chamada pelo painel do TryStac — antes dele virar
-- tráfego real). Sem teto pra max_tokens no banco (só `> 0`): o teto real é o
-- clamp do gateway em runtime (MAX_MAX_TOKENS).
--
-- ATENÇÃO ordem de deploy (mesmo aviso da 0050/0053/0055): find_active_key
-- (docker/gateway/supa.py) passa a pedir estas 2 colunas no select aninhado
-- de stacks, e coluna inexistente vira PostgREST 400 dentro de um
-- raise_for_status — 500 em 100% do tráfego. Esta migration tem que estar
-- aplicada ANTES do deploy do gateway.
alter table stacks
  add column if not exists default_max_tokens integer
    check (default_max_tokens is null or default_max_tokens > 0),
  add column if not exists default_presence_penalty numeric
    check (default_presence_penalty is null or (default_presence_penalty >= -2 and default_presence_penalty <= 2));
