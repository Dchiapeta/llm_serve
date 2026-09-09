-- Defaults de GERAÇÃO DE IMAGEM por CHAVE: o que a 0062 fez para a stack,
-- agora um nível acima — mesmo desenho de 0055 (sampling por chave) sobre
-- 0035/0056 (sampling por stack).
--
-- O caso que motiva: uma conta com várias chaves, uma por cliente ou por
-- superfície do produto, que precisa de proporção ou custo diferentes em cada
-- uma sem criar uma stack nova para isso. A stack segue valendo como o default
-- de quem não configurou nada na credencial.
--
-- Sem switch (mesmo motivo da 0055): NULL já significa "sem override" sem
-- ambiguidade, porque não há rascunho a preservar num número.
--
-- Precedência (docker/gateway/main.py, apply_key_image_defaults chamada logo
-- antes de apply_stack_image_defaults): valor do CLIENTE no corpo ganha de
-- tudo; depois a CHAVE; depois a STACK; e por último os defaults do pod
-- (IMAGE_DEFAULT_SIZE/IMAGE_STEPS/IMAGE_GUIDANCE_SCALE). Um valor de chave
-- aplicado aqui passa a ocupar a posição de "o cliente mandou" para o resto do
-- fluxo, exatamente como em apply_key_sampling_defaults.
--
-- Os CHECKs são os mesmos da 0062, e pelo mesmo motivo: espelham o que o pod
-- aceita (ALLOWED_SIZES, STEPS_MAX=8, validate_guidance_scale) para pegar erro
-- de quem ESCREVE — aqui, o dialog de configurações da chave no painel do
-- TryStac, que escreve direto no Supabase via RLS — antes de virar tráfego.
--
-- Do lado do TryStac falta o grant coluna-a-coluna para `authenticated`
-- (migration própria daquele repo, 0039_api_keys_image_overrides_policy.sql),
-- mesmo par que 0055/0032 formam para sampling: sem o de SELECT a query da
-- página de chaves falha inteira; sem o de UPDATE o dialog salva "com sucesso"
-- sem gravar nada.
--
-- ATENÇÃO ordem de deploy (mesmo aviso da 0053/0055/0062): find_active_key
-- (docker/gateway/supa.py) passa a pedir estas 3 colunas no select, e coluna
-- inexistente vira PostgREST 400 dentro de um raise_for_status — 500 em 100%
-- do tráfego. Esta migration tem que estar aplicada ANTES do deploy do gateway.
alter table api_keys
  add column if not exists default_image_size text
    check (default_image_size is null or default_image_size in ('1024x1024', '1536x1024', '1024x1536')),
  add column if not exists default_image_steps integer
    check (default_image_steps is null or (default_image_steps >= 1 and default_image_steps <= 8)),
  add column if not exists default_image_guidance_scale numeric
    check (default_image_guidance_scale is null or (default_image_guidance_scale >= 0 and default_image_guidance_scale <= 20));
