-- Defaults de sampling (temperature/top_p) por stack.
--
-- Motivação: até aqui temperature/top_p eram passthrough puro do cliente
-- (gateway só faz clamp de segurança, ver validate_body em
-- docker/gateway/main.py) — não existia nenhum jeito de o admin/conta fixar
-- um comportamento padrão do modelo por stack. Null = mantém o passthrough
-- puro de hoje (cliente manda ou não manda, sem default aplicado).
--
-- Os limites do check espelham o clamp que já existe no gateway.

alter table stacks add column if not exists default_temperature numeric
  check (default_temperature is null or (default_temperature >= 0 and default_temperature <= 2));
alter table stacks add column if not exists default_top_p numeric
  check (default_top_p is null or (default_top_p >= 0 and default_top_p <= 1));
