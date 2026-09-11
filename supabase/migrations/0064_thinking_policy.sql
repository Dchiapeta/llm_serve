-- Thinking deixa de ser efeito colateral do teto de saída.
--
-- Até aqui as duas decisões eram a mesma: max_tokens abaixo de MIN_MAX_TOKENS
-- desligava o raciocínio (ver o comentário do piso em main.py). Isso atendia o
-- cliente que pede pouco de propósito, mas tornava impossíveis as duas
-- combinações legítimas — resposta longa SEM raciocínio (atendimento, que é
-- onde o thinking vira 90% da geração e da latência) e resposta curta COM
-- raciocínio. A escolha passa a ser configuração, não consequência.
--
-- NULL é herdar/legado, não "desligado". É o que garante que nenhuma stack
-- existente mude de comportamento ao aplicar esta migration: quem não foi
-- configurado continua exatamente como estava, e a migração é stack a stack.
--
-- Duas colunas porque a precedência é request → chave → stack → legado: a
-- mesma stack serve a integração que precisa raciocinar e a que não precisa,
-- sem o cliente mandar parâmetro em toda request (mesma lógica do system
-- prompt por chave, 0053).
--
-- ORDEM DE DEPLOY: esta migration ANTES do gateway. O supa.py passa a pedir
-- default_enable_thinking no select de api_keys/stacks, e sem a coluna o
-- PostgREST recusa a query inteira — o que derruba a resolução de chave, ou
-- seja, todo o tráfego, não só o thinking.
alter table public.stacks
  add column if not exists default_enable_thinking boolean;
alter table public.api_keys
  add column if not exists default_enable_thinking boolean;

comment on column public.stacks.default_enable_thinking is
  'NULL = legado/herdar; false = resposta direta; true = raciocínio. Request e chave têm precedência.';
comment on column public.api_keys.default_enable_thinking is
  'NULL = herda a stack; booleano sobrescreve a stack. Request explícita tem precedência.';
