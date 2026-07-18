# Playbook de teste de carga por plano

Como testamos um template de plano (VibeCoder, Pro, Max, Enterprise) antes de
liberar pra produção. Objetivo: sempre sair com os mesmos números
comparáveis entre planos — tempo de resposta por dificuldade/categoria,
throughput sob concorrência, e taxa de erro real.

## Pré-requisitos

1. **A stack de teste precisa ter `api_keys.stack_id` preenchido.** Sem
   isso, o gateway roteia pelo heurístico legado por `accounts.plan`, que
   quebra se a conta de teste tiver stacks de outros planos (ver
   "Armadilhas" abaixo). Confirme com:
   ```sql
   select stack_id from api_keys where key_hash = '<hash da chave>';
   ```
2. **A máquina precisa estar `running`.** Subir/religar é ação de quem tem
   acesso à RunPod (custo real) — nunca automatizar isso sem confirmação.
3. **Sempre testar via o gateway** (`https://llmserve-docker.up.railway.app`),
   nunca direto no `public_url` do pod. Ver "Armadilhas" — bater direto no
   pod tem dois problemas: não atualiza `last_activity_at` (a máquina pode
   ser auto-pausada no meio do teste) e não exercita o roteamento/realocação
   real que um cliente usaria.

## Metodologia

- **Concorrência**: 5, 10 e 15 usuários simultâneos (ajustar o teto pro
  `max_users` do template, se for menor que 15).
- **Por usuário**: 5 requisições **sequenciais** (não em paralelo consigo
  mesmo) — simula uma sessão real de uso, não só uma rajada única.
- **Pool de tarefas**: 4 categorias × 3 dificuldades — coding, debugging,
  matemática, raciocínio lógico (think), em fácil/médio/difícil. O pool
  padrão de 18 tarefas está em `scripts/loadtest.py` (`TASKS`) — pode
  crescer, mas mantenha a distribuição por categoria/dificuldade.
- **`max_tokens`**: sempre generoso (8000). Budgets baixos cortam a resposta
  no meio do raciocínio antes de chegar na resposta final ("problema do
  length", achado na validação do VibeCoder) — isso mede o teto de tokens,
  não a qualidade do modelo.
- **Sempre streaming** (`stream: true`). Requisição não-streaming através do
  gateway pode estourar o timeout de leitura (60s, é só o idle timeout do
  proxy, não um teto de duração total — mas sem streaming a resposta inteira
  chega de uma vez só, e uma tarefa difícil facilmente passa de 60s).
- **Cenário de isolamento de slot (multi-tenant)**: além da concorrência
  dentro de uma única stack, rodar N contas de teste diferentes (cada uma
  com seu `stack_id`/slot próprio) simultaneamente na **mesma máquina**,
  cada uma disparando sua própria sequência de 5 requisições. Objetivo:
  medir se a atividade de uma conta degrada a latência/throughput de outra
  conta que divide a mesma GPU — é o que valida (ou não) as razões de
  oversubscription assumidas para cada plano (2-4x VibeCoder, 5-10x
  Pro/Max).
  - Rodar em dois modos pra comparação: (a) baseline — cada conta sozinha
    na máquina, sem vizinhos; (b) full — todas as N contas ativas ao mesmo
    tempo. A diferença entre (a) e (b) é o "custo do vizinho" por conta.
  - N de contas simultâneas deve escalar até a razão de oversubscription
    assumida do plano (ex: testar com 5 e 10 contas simultâneas num
    template Pro pra validar a hipótese de 5-10x).

## Como rodar

```bash
pip install httpx
python3 scripts/loadtest.py \
  --base-url https://llmserve-docker.up.railway.app \
  --api-key <chave HEX da stack de teste> \
  --model <served-model-name do template, ex: pro-base> \
  --levels 5,10,15 \
  --out resultados_<plano>.json
```

Roda os 3 níveis em sequência (não em paralelo entre si), salvando parcial
a cada nível concluído. Espere: cada nível pode levar 10-25 minutos
dependendo da dificuldade média das tarefas sorteadas e da concorrência.

Cenário de isolamento de slot (N contas distintas na mesma máquina, ver
§Metodologia): passe as chaves das contas em `--isolation-keys` — o script
roda baseline (cada conta sozinha) e depois full (todas simultâneas), com
as mesmas tarefas nos dois modos, e imprime o "custo do vizinho" por conta:

```bash
python3 scripts/loadtest.py \
  --base-url https://llmserve-docker.up.railway.app \
  --isolation-keys <chave1>,<chave2>,<chave3> \
  --model pro-base \
  --out isolamento_pro.json
```

## Métricas a reportar

Sempre os mesmos cortes, pra comparar entre plano e entre rodadas:

- **Por nível de concorrência**: taxa de erro, tempo médio/mediana/min/max,
  throughput agregado (tokens/s).
- **Por categoria**: tempo médio, tokens médios.
- **Por dificuldade**: tempo médio, tokens médios.
- **Categoria × dificuldade**: tabela cruzada (tempo médio, n).
- **Erros**: tipo (`ReadError`, `ReadTimeout`, `HTTP 5xx`, etc.), fase
  (`pre_byte` = infra do teste, retentável; `mid_stream` = perda real), em
  qual posição da sequência de 5 aconteceram, e se concentram em alguma
  categoria/dificuldade específica (ver armadilha 5).
- **`finish_reason`**: taxa de `length` (truncamento pelo teto de tokens)
  separada da taxa de `stop` (resposta completa).
- **Isolamento entre contas**: tempo médio/mediana por conta no modo
  baseline vs. modo full (N contas simultâneas), e o delta percentual
  entre os dois — esse delta é o indicador direto de quão bem o slot
  isola uma conta da atividade das outras.

Estrutura de relatório usada até aqui (bom padrão a repetir): resumo em
cards por nível (taxa de erro colorida + tempo/throughput), um gráfico de
barra simples da taxa de erro por nível, 3-5 achados em prosa com números
reais, tabelas de apoio, recomendações. Ver relatório do Pro (17/07/2026)
como referência de formato.

## Armadilhas conhecidas

Descobertas rodando o teste do plano Pro pela primeira vez — evitar repetir:

1. **Testar direto no pod pausa a máquina no meio do teste.**
   `last_activity_at` só é atualizado pelo `maybe_touch()` do gateway —
   bypassar o gateway deixa a máquina "invisível" pro sistema de
   auto-pausa, que derruba o pod mesmo com GPU ativa processando. Sempre
   testar via gateway.

2. **Conta de teste com stacks de planos diferentes quebra o roteamento
   legado.** Se a chave não tem `stack_id` (migration 0019), o gateway
   escolhe a stack "casa" pelo `accounts.plan` — numa conta que já testou
   vários planos, isso pode nunca resolver pro plano certo. Fix permanente
   já aplicado (chave carrega `stack_id` desde a criação); só importa pra
   chaves antigas.

3. **Cache de chave do gateway leva até 60s pra propagar.** Se você
   editar `stack_id`/plano de uma chave direto no banco, o `key_cache` do
   gateway (`KEY_CACHE_TTL_S`, default 60s) pode servir a versão antiga
   por até 1 minuto. Espere ou use `/admin/flush-key-cache`.

4. **Cooldown de 120s entre tentativas de auto-wake.** Bater a mesma
   máquina pausada repetidamente em menos de 2 minutos faz a 2ª+ tentativa
   de wake ser ignorada silenciosamente (retorna igual a "sem máquina
   disponível", sem religar nada). Espere o cooldown entre tentativas.

5. **Rajada de erros sob concorrência alta ≠ falta de capacidade da GPU —
   diagnostique antes de concluir.** No teste do Pro (17/07/2026), 15
   concorrentes teve 40% de erro, mas o throughput por request caiu só 9%
   e o vLLM seguiu saudável (checar `/health` do agent no pico de erros
   antes de concluir OOM/crash). O diagnóstico real, fechado com a
   timeline: **um único blip de rede** (t≈4min) resetou TODAS as conexões
   TCP do cliente de teste no mesmo instante — 15 streams ativos cortados
   ao meio — e as requisições seguintes reutilizaram conexões keepalive
   "zumbis" do pool httpx (8 morreram com RST atrasado em ~18s; 7
   penduraram 600s sem receber um byte, sem nunca chegar ao servidor).
   Não era sobrecarga, era 1 evento + fragilidade do pool. Mitigado no
   `scripts/loadtest.py`: keepalive desligado + timeout de silêncio +
   retry pré-primeiro-byte. Nota metodológica: os N "usuários" do teste
   compartilham UMA rede/processo — um blip local vira taxa de erro
   enorme no relatório, quando em produção afetaria só 1 cliente. Por
   isso o script separa `pre_byte` (infra do teste, retentável) de
   `mid_stream` (perda real que um cliente veria).

   **Método de diagnóstico usado (repetir quando houver rajada de erros):**
   - Reconstruir a timeline por usuário (requests são sequenciais por
     usuário: `start(n) = soma das durações anteriores`) e ver se os
     erros compartilham o MESMO instante de fim → evento único vs
     degradação gradual.
   - Cruzar com `usage_metrics` no Supabase (requests/tokens/pico que o
     POD registrou) → quantas requisições chegaram de fato ao servidor.
   - `GET /health` do agent e `uptime_s` do gateway na janela → descarta
     crash/restart de cada componente.

6. **Requisição sem streaming pode estourar o timeout de leitura do
   gateway.** O `proxy_client` do gateway tem 60s de read timeout — é só
   pra pegar conexão zumbi rápido, não um teto de duração (com streaming,
   cada chunk reseta o timer). Sem `stream: true`, a resposta inteira
   chega de uma vez, e qualquer tarefa acima de 60s de geração falha ali.

## Testes de hardening (verificar que os controles barram o esperado)

Diferente da metodologia acima (que mede desempenho sob carga legítima),
este bloco confirma que cada defesa introduzida no hardening de produção
(isolamento entre tenants, allowlist, pinning de modelo, limites,
rate-limit/quota, expiração de chave) efetivamente **barra** o
comportamento que deveria barrar — e que tráfego legítimo continua
passando normalmente. Rodar pelo menos uma vez após qualquer mudança no
gateway/agent, e sempre antes de abrir um template novo pra usuários reais.

**Use uma conta/stack de TESTE dedicada, nunca uma de cliente real** — os
checks de rate-limit/concorrência disparam rajadas de propósito e podem
consumir a quota diária de tokens da conta.

### Automatizado: `scripts/security_check.py`

```bash
# checks rápidos (sem custo de rajada)
python3 scripts/security_check.py \
  --base-url https://llmserve-docker.up.railway.app \
  --api-key <chave HEX da stack de teste>

# inclui rate-limit/concorrência — custo real de GPU, ver aviso no --help
python3 scripts/security_check.py \
  --base-url https://llmserve-docker.up.railway.app \
  --api-key <chave HEX da stack de teste> \
  --include-burst
```

Cobre: `/health` não derruba com 500 (regressão do bug crítico do
middleware de headers), headers de segurança presentes e `Server` não
vazando, rotas `/admin/*` rejeitando sem/com secret errado, allowlist
bloqueando `load_lora_adapter`/`unload_lora_adapter`/`tokenize`,
`/v1/models` sem vazar `acct-*` de outros tenants, pinning de modelo
(manda um `model` forjado e confirma que não quebra nem vaza pra outro
adapter), limite de tamanho de corpo (413) e de número de mensagens (400),
`/v1/messages` (Claude Code) e `/v1/responses` (Codex) respondendo no
formato certo, e (com `--include-burst`) rate-limit disparando 429. O
check de concorrência também roda com `--include-burst`, mas **não espera
mais 429 de uma rajada de uma chave só** — concorrência é elástica por
máquina (ver item 6 abaixo), então uma chave sozinha pode legitimamente
absorver a rajada inteira sem ser barrada; o check só falha se aparecer a
mensagem do antigo teto fixo por chave (sinal de deploy desatualizado). Sai
com código 1 se algum check falhar — dá pra plugar num CI/step manual de
release.

### Manual: precisam de setup no banco (não automatizados)

**1. Isolamento de RAG entre stacks da mesma conta.** Indexe um documento
característico numa stack A (upload pelo painel), depois mande uma
pergunta pela chave da stack B (mesma conta) que só faria sentido
responder citando o conteúdo de A. Confirme que a resposta não reflete
esse conteúdo. Verificação direta no banco (todo chunk deve ter
`stack_id` da própria stack, nunca `null`, numa conta com 2+ stacks):

```sql
select id, stack_id, left(content, 60) as preview
from knowledge_chunks
where account_id = '<account_id da conta de teste>'
order by stack_id nulls first;
```

Qualquer linha com `stack_id` null aqui é um chunk legado ainda não
reindexado — inacessível via RAG (não vaza, mas também não é servido).

**2. Fail-closed de chave sem stack_id.** Depois da migration 0021, toda
chave ativa de conta com stacks deveria ter `stack_id` resolvido — esta
query deveria sempre voltar vazia:

```sql
select ak.id, ak.key_prefix, ak.account_id
from api_keys ak
where ak.status = 'active' and ak.stack_id is null
  and exists (select 1 from stacks s where s.account_id = ak.account_id);
```

Se voltar alguma linha, teste a chave correspondente contra o gateway —
espera-se `401 chave sem stack associada`.

**3. Expiração de chave.** Force o vencimento de uma chave de teste:

```sql
update api_keys set expires_at = now() - interval '1 day'
where key_hash = '<hash da chave de teste>';
```

Espera-se `401 chave expirada` no gateway. Pra confirmar a segunda camada
(enforcement direto no agent, contra o bypass de bater na URL do pod), a
chave precisa já ter sido re-sincronizada com o `expires_at` novo (o
próximo `sync-keys`/`upsert-keys` propaga) — teste batendo direto no
`public_url` da máquina. **Restaure depois**, senão a chave de teste fica
inutilizável:

```sql
update api_keys set expires_at = null where key_hash = '<hash da chave de teste>';
```

**4. Isolamento entre contas (cross-account).** Essa fronteira é
estrutural (todo roteamento e toda query de RAG são filtrados por
`account_id` derivado da própria chave) — não dá pra "provar" batendo de
fora sem já ter a chave da outra conta, o que anularia o teste. Validação
prática: se você tem duas contas de teste reais, confirme que a resposta
de uma nunca reflete o `system_prompt`/RAG/adapter da outra (mesma
metodologia do item 1, mas com contas em vez de stacks).

**5. Recuperação de máquina `terminated`/rota presa.** Não force isso
artificialmente contra uma máquina real (custo/risco desnecessário).
Observar durante operação real: se uma máquina for encerrada pelo console
da RunPod com rotas ativas, as contas afetadas devem se recuperar
sozinhas no próximo request (realocação ou auto-wake), sem ficar presas
em 503 indefinidamente. Uma rota presa em `loading`/`migrating` por mais
de 30 minutos deve aparecer nos logs do gateway como `reconciliação: conta
... presa em '...' — liberada` (loop a cada 5 min, ver
`STALE_ROUTE_CHECK_INTERVAL_S`/`STALE_ROUTE_THRESHOLD_S`).

**6. Concorrência elástica por máquina + piso reservado (pod compartilhado).**
Concorrência não trava mais por chave — trava pelo agregado de
`in_flight` na MÁQUINA vs. `machines.max_concurrent_seqs` (migration 0028;
`NULL` cai no fallback `DEFAULT_MAX_CONCURRENT_SEQS`), com um piso
(`MIN_RESERVED_SLOTS_SHARED_POD`, default 2) sempre reservado em planos de
pod compartilhado (`SHARED_POD_PLANS` — VibeCoder/Pro) pra quem chegar
depois nunca ficar 100% bloqueado esperando um tenant pesado. O roteamento
(`routing_state`) é por `account_id`, não por `stack_id` — requer então
duas CONTAS de teste (não duas stacks da mesma conta) cujas chaves roteiem
pra mesma máquina compartilhada:

```sql
-- confirma que as duas contas de teste caem na mesma máquina
select r.account_id, r.machine_id
from routing_state r
where r.account_id in ('<conta_a>', '<conta_b>');
```

- Dispare uma rajada sustentada só com a chave A (ex.: `security_check.py
  --include-burst` ou N requests concorrentes manuais) até ela sozinha
  ocupar perto da capacidade cheia da máquina.
- Enquanto isso, mande UMA requisição com a chave B. Espera-se que ela
  **não** receba 429 — o piso reservado garante isso mesmo com A saturando
  o resto. Confirme via `GET /admin/routes` (header `X-Admin-Secret`) que
  `in_flight_by_machine` do `machine_id` compartilhado nunca ultrapassa
  `max_concurrent_seqs` da máquina (coluna em `machines`, não exposta pelo
  endpoint — conferir direto no banco).
- Em pod dedicado (Max/Enterprise), o mesmo teste com a máquina do próprio
  plano deve deixar a chave sozinha ocupar a capacidade cheia, sem sobra
  reservada (não há vizinho pra proteger).