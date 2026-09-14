-- Seed default de GERAÇÃO DE IMAGEM, por STACK e por CHAVE: o quarto parâmetro
-- que faltava ao lado de size/steps/guidance_scale (0062 na stack, 0063 na
-- chave), com a mesma mecânica e a mesma precedência.
--
-- Precedência (docker/gateway/main.py, apply_key_image_defaults e depois
-- apply_stack_image_defaults): `seed` que o CLIENTE manda no corpo ganha; depois
-- a CHAVE; depois a STACK; e por último o pod (IMAGE_DEFAULT_SEED, ou sorteio em
-- policy.ensure_seed).
--
-- Faixa [0, 2^53-1], e não os [0, 2^64-1] que o pod aceita (validate_seed):
-- bigint do Postgres é signed e não comporta 2^64-1, e acima de 2^53 o número é
-- arredondado em silêncio no caminho JS (TryStac -> rota PATCH do painel ->
-- PostgREST). Um subconjunto do que o pod aceita, então nunca vira 400 lá.
--
-- Do lado do TryStac falta o grant coluna-a-coluna em api_keys para
-- `authenticated` (migration 0043_api_keys_image_seed_policy.sql daquele repo),
-- mesmo par que 0063/0039 formam.
--
-- ATENÇÃO ordem de deploy (mesmo aviso da 0062/0063): find_active_key
-- (docker/gateway/supa.py) passa a pedir esta coluna nas duas alturas do select,
-- e coluna inexistente vira PostgREST 400 dentro de um raise_for_status — 500 em
-- 100% do tráfego. Esta migration tem que estar aplicada ANTES do deploy do
-- gateway.
alter table stacks
  add column if not exists default_image_seed bigint
    check (default_image_seed is null or (default_image_seed >= 0 and default_image_seed <= 9007199254740991));

alter table api_keys
  add column if not exists default_image_seed bigint
    check (default_image_seed is null or (default_image_seed >= 0 and default_image_seed <= 9007199254740991));
