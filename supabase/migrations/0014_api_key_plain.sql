-- Armazena a chave em texto puro para permitir copiá-la depois da criação.
-- Trade-off consciente: quem tiver acesso ao banco lê as chaves. O hash
-- continua sendo usado na autenticação; chaves antigas ficam com null
-- (só o prefixo é recuperável).
alter table api_keys add column plain_key text;
