-- Rollup de leitura sobre image_generations: expõe consumo de geração de
-- imagem (contagem de imagens e de requisições) na mesma forma que
-- usage_metrics expõe consumo de tokens, para que o painel some a unidade
-- certa por stack/conta/máquina em vez de ler sempre tokens_in/tokens_out.
--
-- ---------------------------------------------------------------------------
-- Por que existe
-- ---------------------------------------------------------------------------
-- Geração de imagem não produz token nenhum: gateway_requests.tokens_in/out
-- fica NULL de propósito nas rotas de imagem (main.py,
-- _relay_image_response: "usage=None sempre... null é a ausência da
-- contagem, zero seria uma contagem"), e usage_metrics grava requests>0 com
-- tokens zerados. Toda tela que soma "consumo" soma tokens_in+tokens_out, e
-- uma stack de imagem aparece com 0 mesmo tendo gerado centenas de imagens.
-- O dado certo já existe em image_generations (0059) — este rollup é só a
-- leitura agregada dele, sem tocar o gateway nem o pod.
--
-- ---------------------------------------------------------------------------
-- Por que uma VIEW, e não uma tabela alimentada por coletor
-- ---------------------------------------------------------------------------
-- usage_metrics existe como TABELA porque o coletor do gateway zera contadores
-- em memória do agent na leitura (docker/gateway/main.py,
-- collect_usage_metrics_once) — sem persistir o delta, ele se perde. Imagem
-- não tem esse problema: cada linha de image_generations já é permanente,
-- gravada no caminho crítico da própria requisição (comentário da 0059). Uma
-- view soma direto da fonte — sem processo de coleta, sem janela perdida se
-- o gateway cair entre coletas, e sem uma segunda verdade que possa divergir
-- da tabela de artefato.
--
-- ---------------------------------------------------------------------------
-- Por hora, não por dia
-- ---------------------------------------------------------------------------
-- dashboard-body.tsx faz bucket em America/Sao_Paulo. Hora UTC mapeia 1:1
-- para hora de SP (sem DST desde 2019); dia UTC NÃO mapeia para dia de SP. A
-- dobra para granularidade diária continua acontecendo em TypeScript, como já
-- é feito para usage_metrics — agregar por dia aqui introduziria erro de
-- fuso silencioso.
--
-- `window_start` é o mesmo nome de usage_metrics.window_start de propósito:
-- os filtros `.gte("window_start", periodStart)` já escritos em
-- app/(dashboard)/stacks/page.tsx, contas/page.tsx, dashboard-body.tsx e
-- crm/queries.ts valem sobre esta view sem reescrever a lógica de janela.
--
-- ---------------------------------------------------------------------------
-- security_invoker = true não é estilo, é a fronteira de RLS
-- ---------------------------------------------------------------------------
-- Sem ele, uma view sobre tabela com RLS roda com os direitos do DONO da view
-- e contorna a RLS de quem tiver select nela — e o Supabase concede select
-- por default a anon/authenticated em objetos novos do schema public. Com
-- invoker, a service role (que ignora RLS) continua vendo tudo; authenticated
-- fica limitado à policy que a 0059 deixou intencionalmente vazia — falha
-- fechado por construção. O revoke abaixo é redundante com o invoker mas
-- documenta a intenção: nada deve ler esta view por engano antes de o TryStac
-- (que divide este projeto Supabase) decidir abrir acesso do lado dele.
--
-- ---------------------------------------------------------------------------
-- images vs image_requests
-- ---------------------------------------------------------------------------
-- Uma requisição pode virar N linhas com o mesmo batch_id (hoje
-- IMAGE_IMAGES_PER_REQUEST_MAX=1 no pod, mas é env — docker/image/server.py).
-- `images` = count(*), a unidade de consumo real; `image_requests` =
-- count(distinct batch_id). As duas coexistem porque usage_metrics.requests
-- conta o que o pod VIU (inclusive falhas), e image_requests conta o que
-- PRODUZIU imagem armazenada — misturar as duas fontes na mesma coluna faria
-- linhas vizinhas contarem coisas diferentes.

-- Índice por máquina: não existia (0059 só indexa por stack_id, account_id,
-- api_key_id, batch_id e expires_at). É o acesso que o CRM (rateio de custo
-- de GPU por máquina) e machines/[id] (uso por chave dentro de uma máquina)
-- fazem.
create index if not exists image_generations_machine_idx
  on image_generations(machine_id, created_at desc);

create or replace view image_usage_rollup
  with (security_invoker = true) as
  select
    stack_id,
    account_id,
    api_key_id,
    machine_id,
    date_trunc('hour', created_at) as window_start,
    count(*) as images,
    count(distinct batch_id) as image_requests
  from image_generations
  group by stack_id, account_id, api_key_id, machine_id,
           date_trunc('hour', created_at);

revoke all on image_usage_rollup from public, anon;
