-- Plano comercial e categoria de workload são eixos diferentes.
--
-- O Go pode existir tanto como produto de LLM quanto como produto de imagem.
-- Usar apenas `plan` faria uma stack Go de imagem cair em qualquer máquina Go,
-- inclusive num pod vLLM. `category` acompanha a stack mesmo quando ela fica
-- temporariamente sem máquina, permitindo que wake/recreate/provisionamento
-- escolham novamente o tipo correto de template.

alter table templates
  add column if not exists category text not null default 'llm';

alter table stacks
  add column if not exists category text not null default 'llm';

-- Enquanto escritores antigos ainda existem, qualquer insert/update legado
-- com plan=Image precisa nascer classificado corretamente. Sem o trigger, o
-- default llm criaria uma stack que o gateway novo recusaria nas rotas de
-- imagem durante a janela entre EXPAND e CONTRACT.
create or replace function public.classify_legacy_image_category()
returns trigger
language plpgsql
as $$
begin
  if new.plan = 'Image' then
    new.category := 'image';
  end if;
  return new;
end;
$$;

drop trigger if exists templates_classify_legacy_image on templates;
create trigger templates_classify_legacy_image
before insert or update of plan on templates
for each row execute function public.classify_legacy_image_category();

drop trigger if exists stacks_classify_legacy_image on stacks;
create trigger stacks_classify_legacy_image
before insert or update of plan on stacks
for each row execute function public.classify_legacy_image_category();

-- Fase EXPAND: classifica o produto e aplica os limites, mas mantém plan=Image
-- enquanto o gateway antigo ainda pode estar atendendo. O plano só vira Go na
-- migration 0061, depois do deploy do gateway category-aware.
update templates
set category = 'image',
    env = env || jsonb_build_object(
      'IMAGE_DEFAULT_SIZE', '1024x1024',
      'IMAGE_ALLOWED_SIZES', '1024x1024,1536x1024,1024x1536',
      'IMAGE_IMAGES_PER_REQUEST_MAX', '1',
      'IMAGE_MAX_REFERENCE_IMAGES', '4',
      'IMAGE_ALLOWED_FORMATS', 'png,jpeg,webp',
      'IMAGE_MAX_FILE_SIZE_MB', '5',
      'IMAGE_QUEUE_CAPACITY', '3',
      'IMAGE_QUEUE_WAIT_TIMEOUT_S', '60'
    )
where plan = 'Image' or name = 'GO-IMAGE-A40';

update stacks s
set category = 'image'
where s.plan = 'Image'
   or exists (
     select 1
     from machines m
     join templates t on t.id = m.template_id
     where m.id = s.machine_id
       and t.category = 'image'
   );

alter table templates drop constraint if exists templates_category_valid;
alter table templates
  add constraint templates_category_valid check (category in ('llm', 'image'));

alter table stacks drop constraint if exists stacks_category_valid;
alter table stacks
  add constraint stacks_category_valid check (category in ('llm', 'image'));

create index if not exists templates_plan_category_idx
  on templates (plan, category)
  where is_enabled = true and is_test = false;

create index if not exists stacks_plan_category_idx
  on stacks (plan, category);

comment on column templates.category is
  'Tipo de workload servido pelo template: llm ou image. Independente do plano comercial.';

comment on column stacks.category is
  'Tipo de workload contratado pela stack: llm ou image. Preservado mesmo sem machine_id.';
