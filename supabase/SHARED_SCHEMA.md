# Schema compartilhado com o TryStac

Este projeto Supabase (`ovesssnxmcegsbcxhvus`) é usado por **dois repositórios
com históricos de migration independentes**:

- `runpod_llm` (este repo) — `supabase/migrations/`, numeração `0001`–`00NN`.
- `TryStac` (LP + admin do usuário final) — migrations próprias, numeração
  própria (ex.: `0001_auth_rls.sql`, `0010_stacks_name.sql`), em outro repo.

Nenhum dos dois históricos enxerga o do outro. Isso já causou colunas e
policies "fantasma" — existem no banco real, mas em nenhuma migration deste
repo. **Antes de alterar, renomear ou remover qualquer coluna/policy nas
tabelas abaixo, rode `diagnostics.sql` (ou equivalente) contra o banco real —
não confie só no que está em `supabase/migrations/` aqui.**

## Tabelas tocadas pelos dois lados

| Tabela | O que o TryStac adicionou (fora deste repo) |
|---|---|
| `accounts` | `accounts_select_own` (RLS select, `user_id = auth.uid()`) + `grant select ... to authenticated` |
| `stacks` | coluna `name`; `stacks_update_own` (RLS update) + grants **coluna-a-coluna** (`name`, `system_prompt`) para `authenticated`. **Diferente das demais colunas desta linha:** `default_temperature`/`default_top_p` (`0035` daqui) e `default_max_tokens`/`default_presence_penalty` (`0056` daqui) **não têm grant de update pro TryStac** — a leitura vem do `grant select` de tabela inteira que o TryStac já concedeu (fora deste repo), e a escrita é feita por **este repositório**, via `PATCH /api/stacks/[id]/model-config` (service role, sem RLS) — o painel do TryStac chama essa rota em vez de escrever direto no Supabase. Não "corrigir" isso adicionando um grant coluna-a-coluna achando que falta um — é intencional. |
| `usage_metrics` | `usage_metrics_select_own_stack` (RLS select; desde a `0017` de lá resolve por `usage_metrics.stack_id` direto, sem passar por `api_keys`) e a view `stack_token_totals`. **A FK `api_key_id → api_keys` é `on delete set null` desde a `0066` daqui** (era `cascade` na `0001`: apagar a chave levava junto todo o uso dela, e o painel de lá perdia os tokens). Nunca voltar a `cascade`; toda agregação de uso deve ler por `stack_id`, não por `api_keys`. |
| `knowledge_chunks` | policy de SELECT por conta (`authenticated`) + view `stack_knowledge_files` (agregada por `storage_path`, `security_invoker=true`, sem a coluna `embedding`) |
| `api_keys` | colunas `name`, `last_used_at` (uso ainda não identificado neste repo); **`status` aceita `active`, `revoked` e `deleted`** (CHECK `not valid` na `0066` daqui) — `deleted` é o soft delete do painel de lá (`0041` de lá revoga o DELETE de `authenticated`; a `0006` de lá o concedia). Tudo que decide se a chave funciona ou conta (gateway `find_active_key`, `syncMachineKeys`, slots, cota) filtra `status = 'active'`, então `deleted` se comporta como `revoked` — só o rótulo do painel muda; grants **coluna-a-coluna** de update (`name`, `status`, e — desde a `0053` daqui — `use_custom_prompt`/`system_prompt`, e desde a `0055` daqui — `default_temperature`/`default_top_p`/`default_max_tokens`/`default_presence_penalty`, e desde a `0065` daqui — `enable_knowledge_base`) para `authenticated`. O grant de `enable_knowledge_base` mora na `0040` do TryStac e **depende da `0065` daqui já estar aplicada**: grant sobre coluna inexistente falha com `42703`. Sem o grant de **select**, a query da página de chaves de lá falha inteira — a coluna fica inacessível, não apenas invisível. **No caminho de volta:** nunca drope `enable_knowledge_base` com o gateway novo no ar — ele a pede no select de `find_active_key`, e a coluna sumindo vira 500 em 100% do tráfego. Reverta o gateway primeiro, a coluna depois (ou nunca: ela é aditiva e nullable, um gateway antigo a ignora). |

## Colunas deste repo escritas pelo lado do TryStac

`stacks.billing_status`, `stacks.past_due_since` e `stacks.provisioning_ref`
são criadas aqui (`0050_stacks_billing.sql`) mas **escritas do outro lado**:

- `billing_status` / `past_due_since` — pelo trigger `project_subscription_to_stack`
  (migration `0029` do TryStac), que projeta `chargefy_subscriptions` na stack.
  Também escritas pelo `billing_reconcile_once` do gateway (`docker/gateway/main.py`),
  que materializa `past_due` vencido → `suspended`. **Nenhuma aplicação escreve
  estas colunas à mão** — trocar isso por um update de aplicação reintroduz o
  problema de ordenação que o trigger resolve (a Chargefy não garante ordem
  entre eventos).
- `provisioning_ref` — pelo `POST /api/stacks` deste repo, com o
  `client_reference` que o TryStac gera em `chargefy_checkout_attempts`. O
  unique parcial é o que impede uma reentrega de webhook virar uma segunda
  stack.

Ordem obrigatória: `0050` (aqui) **antes** de `0029` (lá), e as duas antes de
qualquer deploy do gateway que leia as colunas — `find_active_key` faz
`raise_for_status()`, então coluna faltando vira 500 em todo o tráfego.

### Categoria do produto (`0060`)

`plan` é o tier comercial; `category` é o workload (`llm` ou `image`). O
primeiro produto de imagem é `plan=Go`, `category=image`, template
`GO-IMAGE-A40`. O checkout do TryStac deve enviar `category: "image"` ao
`POST /api/stacks`; ausência continua significando `llm` por compatibilidade.

Rollout sem indisponibilidade:

1. aplicar `0060_product_category.sql` (EXPAND): cria as colunas, classifica o
   produto e atualiza seus limites, mas preserva o plano legado `Image`;
2. publicar painel e gateway category-aware em todas as réplicas;
3. publicar o TryStac enviando `category: "image"` no checkout;
4. aguardar/limpar o cache antigo de chaves;
5. aplicar `0061_go_image_contract.sql` (CONTRACT), convertendo o estado para
   `plan=Go/category=image`.

Não publique o gateway novo antes da 0060: `find_active_key` seleciona
`stacks.category` explicitamente e falha fechado se a coluna ainda não existir.

## Tabelas novas que o TryStac ainda vai precisar acessar

### `image_generations` + bucket `images` (migrations `0058`/`0059` daqui)

Registro de cada imagem gerada por uma stack da categoria `image`: quem gerou (`account_id`,
`stack_id`, `api_key_id`, `machine_id`), com que parâmetros, e onde o arquivo
está no bucket privado `images`. Escrita **só** pelo gateway, no caminho da
própria requisição (`docker/gateway/image_gen.py`).

Este repo cria a tabela com RLS habilitada e **sem policy**, como todas as
outras. Para o app do cliente listar as próprias imagens, o TryStac precisa
adicionar, do lado dele:

- `image_generations_select_own` — SELECT resolvendo a posse pelo join
  `image_generations.stack_id → stacks.account_id → accounts.user_id = auth.uid()`,
  no mesmo formato de `usage_metrics_select_own_stack`;
- `grant select on image_generations to authenticated`.

**Nenhum grant de INSERT/UPDATE/DELETE**: as linhas descrevem o que o gateway
gravou, e um cliente que pudesse editá-las poderia atribuir a própria geração a
outra stack. A expiração (`file_deleted_at`, e o `prompt` sendo apagado junto) é
feita pelo reaper do gateway com service role.

**A leitura do arquivo é por signed URL de TTL curto** — o bucket é privado, e
`getPublicUrl` não funciona nem deve ser tentado. O padrão é o de
`buildLoraSignedFiles` (`lib/actions.ts`) e `signed_image_url`
(`docker/gateway/supa.py`). Uma policy de `storage.objects` para `authenticated`
seria o caminho alternativo, mas aí a posse teria que ser derivada do prefixo do
path (`{stack_id}/...`), o que é mais frágil que assinar no servidor.

**Prazo:** o arquivo some aos 30 dias; a linha fica. Uma UI que assuma "linha
existe ⇒ imagem existe" vai mostrar link quebrado — o campo a checar é
`file_deleted_at is null`.

## Convenção usada pelo TryStac (pra não colidir nomes/policies)

- Grants de UPDATE são sempre **por coluna** (`grant update (col) on table to authenticated`), nunca a tabela inteira — o resto das colunas (`plan`, `machine_id`, `account_id`, `usage_class`, etc.) continua sem grant nenhum pra `authenticated`.
- Policies de acesso do usuário final seguem o padrão `<tabela>_<ação>_own` (`accounts_select_own`, `stacks_update_own`) — resolvendo a posse sempre por `auth.uid() = accounts.user_id`, direto ou via join.
- RLS dessas tabelas neste repo é habilitada **sem** nenhuma policy (`0001_init.sql`, `0011_knowledge_base.sql`, `0012_stacks.sql`) — todo acesso de `authenticated` vem de policies do lado do TryStac, não daqui.

## O que fazer ao mexer nessas tabelas neste repo

- Alterar/remover uma coluna usada pelas policies acima (`account_id`, `user_id`, `stack_id`) sem avisar o TryStac quebra o acesso do usuário final silenciosamente.
- Uma migration nova aqui que crie policy/grant com o mesmo nome de uma já existente do lado do TryStac vai falhar (ou pior, sobrescrever sem avisar) — checar antes.
