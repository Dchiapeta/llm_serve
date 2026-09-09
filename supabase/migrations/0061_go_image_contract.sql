-- Fase CONTRACT do rollout Go/image: fecha o contrato em plan=Go/category=image
-- e remove o andaime de compatibilidade que a 0060 montou.
--
-- Aplicar depois de 0060_product_category.sql e do deploy do painel e do
-- gateway category-aware em todas as réplicas.
--
-- Nota de realidade, para quem ler isto depois: em produção o produto de imagem
-- nunca chegou a existir com plan='Image' — o template já foi criado como
-- plan=Go/category=image. Os dois UPDATEs abaixo são, portanto, no-op aqui, e
-- existem só para o caso de um ambiente ter parado no estado intermediário que
-- a 0060 permite. O efeito real desta migration em produção são os DROPs do
-- fim: os triggers da 0060 são criados incondicionalmente e sobreviveriam ao
-- contract se ninguém os derrubasse.

update templates
set plan = 'Go'
where category = 'image'
  and (plan = 'Image' or name = 'GO-IMAGE-A40');

update stacks
set plan = 'Go'
where category = 'image'
  and plan = 'Image';

-- O trigger da 0060 é andaime da janela EXPAND e precisa cair JUNTO com o
-- contract, não depois. Ele classifica category='image' a partir de
-- plan='Image' mas não normaliza o plano; sobrevivendo aos updates acima, um
-- escritor legado passaria a produzir stacks plan=Image/category=image contra
-- um template já convertido para plan=Go. As duas linhas não casariam mais:
-- list_running_machines_for_plan('Image','image') volta vazio, e o fallback de
-- wake/provisionamento termina em 503 permanente — mascarado enquanto o
-- machine_id ainda apontar para uma máquina running, e visível só quando ela
-- pausar. Depois desta migration o único contrato é plan=Go/category=image.
drop trigger if exists templates_classify_legacy_image on templates;
drop trigger if exists stacks_classify_legacy_image on stacks;
drop function if exists public.classify_legacy_image_category();
