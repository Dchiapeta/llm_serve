-- Origem da requisição + conserto da coluna `model` em gateway_requests.
--
-- ---------------------------------------------------------------------------
-- 1. user_agent: de onde a requisição veio
-- ---------------------------------------------------------------------------
-- O `path` já separa dois clientes por construção — Claude Code só fala
-- /v1/messages e o Codex só fala /v1/responses. Mas Cursor, Continue, Cline,
-- os SDKs e curl caem TODOS em chat/completions e ficam indistinguíveis. Como
-- o produto é BYOE (o cliente aponta a ferramenta dele pro endpoint), saber
-- qual ferramenta está do outro lado é o que torna o histórico acionável.
--
-- Guardamos o User-Agent CRU e classificamos na UI (lib/request-origin.ts):
-- a lista de ferramentas muda mais rápido que o schema, e um rótulo gravado
-- aqui congelaria a classificação do dia do insert. É string controlada pelo
-- cliente — o gateway trunca, e nada no servidor toma decisão com base nela.
--
-- Linhas anteriores a esta migration ficam com NULL e a UI cai no fallback
-- por path.
alter table gateway_requests add column if not exists user_agent text;

comment on column gateway_requests.user_agent is
  'User-Agent cru do cliente (truncado pelo gateway). Classificado em rótulo '
  'de origem na UI (lib/request-origin.ts), nunca aqui — a lista de '
  'ferramentas muda mais rápido que o schema. NULL = requisição anterior à '
  'migration 0041, ou cliente que não mandou o header; a UI cai no fallback '
  'por path (messages = Claude Code, responses = Codex).';

-- ---------------------------------------------------------------------------
-- 2. Limpeza do `model` gravado como booleano
-- ---------------------------------------------------------------------------
-- Bug do log fire-and-forget: os dois log_ctx do gateway passavam
-- `model=rewrite_model`, que é o BOOLEANO que decide se o modelo efetivo vira
-- o nome do adapter LoRA da stack. A coluna é text, então PostgREST coagiu
-- para as strings 'true'/'false' e a tela de Requisições exibia "false" como
-- se fosse nome de modelo.
--
-- O nome real dessas linhas não é recuperável (dependia de machine +
-- rewrite_model no momento da request). NULL é o valor honesto: a UI já
-- renderiza "—" para nulo, o que é bem melhor que um valor que mente.
--
-- Idempotente: a segunda execução casa 0 linhas. O conserto do produtor está
-- em docker/gateway/main.py (effective_model_name).
update gateway_requests
set model = null
where model in ('true', 'false');
