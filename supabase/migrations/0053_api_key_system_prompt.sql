-- System prompt POR CHAVE: a mesma stack passa a servir usos diferentes sem o
-- cliente ter que mandar `system` em cada request.
--
-- Até aqui a instrução era propriedade exclusiva da stack (0020) e a única
-- forma de variar por integração era o cliente embutir um `system` no corpo —
-- o que espalha a configuração por todo código que chama a API. Com estas duas
-- colunas a variação mora na credencial: a chave do suporte responde como
-- suporte, a do resumo de e-mail responde como resumidor, e as duas requests
-- continuam sendo só `{"model", "messages"}`.
--
-- Duas colunas em vez de uma só (`system_prompt is not null` = usa o da
-- chave): desligar o prompt próprio no painel não pode apagar o texto que o
-- cliente escreveu — o switch da UI alterna `use_custom_prompt` e o rascunho
-- sobrevive pra ser religado depois.
--
-- default false + nullable: toda chave existente continua exatamente como
-- está (prompt da stack), e a migration é segura de rodar com o gateway antigo
-- no ar. A ORDEM IMPORTA na outra direção — find_active_key (docker/gateway/
-- supa.py) passa a pedir estas colunas no select, e coluna inexistente vira
-- PostgREST 400 dentro de um raise_for_status, ou seja, 500 em 100% do
-- tráfego. Esta migration tem que estar aplicada ANTES do deploy do gateway.
alter table api_keys
  add column if not exists system_prompt text,
  add column if not exists use_custom_prompt boolean not null default false;
