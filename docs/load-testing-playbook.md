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