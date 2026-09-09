-- Defaults de GERAÇÃO DE IMAGEM por stack: o equivalente, para o produto
-- category='image', do que default_temperature/default_top_p/default_max_tokens/
-- default_presence_penalty (0035/0056) já são para o produto de texto.
--
-- Os quatro defaults de sampling não têm significado nenhum nas rotas
-- /v1/images/*: o pod de difusão (docker/image/server.py) lê size, steps e
-- guidance_scale, e ignora tudo o mais. Até aqui a página de Comportamento do
-- TryStac oferecia ao cliente de imagem exatamente os parâmetros que a stack
-- dele não usa; estas três colunas são o que aquela tela passa a escrever.
--
-- Precedência (docker/gateway/main.py, apply_stack_image_defaults): o valor que
-- o CLIENTE manda no corpo ganha; o default da stack só preenche o campo
-- ausente. Não existe camada por CHAVE aqui — as colunas equivalentes em
-- api_keys (0055) são de sampling e não têm par para imagem.
--
-- Os CHECKs espelham a validação do pod, que é quem realmente recusa:
-- ALLOWED_SIZES (IMAGE_ALLOWED_SIZES), validate_steps com maximum=STEPS_MAX=8 e
-- validate_guidance_scale. Rejeitar aqui também pega o erro em quem ESCREVE
-- nestas colunas — a rota PATCH /api/stacks/[id]/model-config deste repo,
-- chamada pelo painel do TryStac — antes de ele virar tráfego real.
--
-- O teto 8 de steps não é arbitrário nem conservador: o checkpoint é destilado
-- e produz imagem pronta em pouquíssimos passos. Gravar 50 aqui seria queimar
-- GPU sem ganho de qualidade.
--
-- ATENÇÃO ordem de deploy (mesmo aviso da 0050/0053/0055/0056): find_active_key
-- (docker/gateway/supa.py) passa a pedir estas 3 colunas no select aninhado de
-- stacks, e coluna inexistente vira PostgREST 400 dentro de um
-- raise_for_status — 500 em 100% do tráfego, inclusive no de texto. Esta
-- migration tem que estar aplicada ANTES do deploy do gateway.
alter table stacks
  add column if not exists default_image_size text
    check (default_image_size is null or default_image_size in ('1024x1024', '1536x1024', '1024x1536')),
  add column if not exists default_image_steps integer
    check (default_image_steps is null or (default_image_steps >= 1 and default_image_steps <= 8)),
  add column if not exists default_image_guidance_scale numeric
    check (default_image_guidance_scale is null or (default_image_guidance_scale >= 0 and default_image_guidance_scale <= 20));
