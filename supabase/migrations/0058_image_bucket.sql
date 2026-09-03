-- Bucket do Storage onde as imagens geradas pelo plano Image são guardadas.
--
-- ---------------------------------------------------------------------------
-- Por que uma MIGRATION, e não o dashboard
-- ---------------------------------------------------------------------------
-- Os dois buckets que já existem ("loras" e "knowledge") nasceram no dashboard
-- do Supabase, e esta é a primeira migration do repo a tocar `storage.buckets`.
-- A mudança de prática é deliberada: aqui o bucket entra na ORDEM DE DEPLOY.
-- Com a persistência síncrona (o gateway sobe a imagem antes de responder), um
-- bucket ausente não é um detalhe que se conserta depois — é 502 em toda
-- geração. Um passo manual numa ordem crítica é exatamente o tipo de coisa que
-- se esquece na primeira vez que se sobe isso em produção.
--
-- Se o role que roda as migrations não tiver permissão no schema `storage`
-- (depende do projeto), este insert falha. Nesse caso: criar o bucket à mão no
-- dashboard, com estes mesmos valores, e seguir. O `on conflict do nothing`
-- deixa a migration válida como no-op num banco onde ele já exista.
--
-- ---------------------------------------------------------------------------
-- Privado, não público
-- ---------------------------------------------------------------------------
-- `public = false`, como os outros dois. A imagem é do cliente que a gerou, e um
-- bucket público a entregaria a quem adivinhasse a URL — que é derivada de
-- stack_id + batch_id, mas ainda assim um segredo por obscuridade. A leitura
-- acontece por signed URL de TTL curto, mesmo caminho de `signed_lora_files`
-- (docker/gateway/supa.py) e de buildLoraSignedFiles (lib/actions.ts).
--
-- Sem policy de RLS aqui, pelo mesmo motivo da tabela image_generations
-- (migration 0059): quem lê é o gateway/painel com a service role, que ignora
-- RLS. As policies para o app do cliente nascem no repo TryStac — ver
-- supabase/SHARED_SCHEMA.md.

insert into storage.buckets (id, name, public)
values ('images', 'images', false)
on conflict (id) do nothing;
