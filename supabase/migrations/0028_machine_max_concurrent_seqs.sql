-- Espelha o --max-num-seqs real passado ao vLLM no deploy de cada pod.
-- Usado pelo gateway como teto de concorrência ELÁSTICO por máquina (substitui
-- o antigo teto fixo por chave, MAX_CONCURRENT_PER_KEY): uma stack sozinha no
-- pod pode ocupar quase toda a capacidade; várias dividem o mesmo teto
-- conforme aparecem. NULL = capacidade desconhecida, gateway cai no fallback
-- global (DEFAULT_MAX_CONCURRENT_SEQS) até o valor real ser preenchido aqui.
alter table machines
  add column if not exists max_concurrent_seqs integer;

comment on column machines.max_concurrent_seqs is
  'Espelha o --max-num-seqs do deploy do pod (VLLM_EXTRA_ARGS/entrypoint.sh). '
  'NULL = gateway usa o fallback global DEFAULT_MAX_CONCURRENT_SEQS.';
