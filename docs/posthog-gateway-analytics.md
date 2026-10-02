# Analytics de uso da API no PostHog

Esta integração usa o ledger `gateway_requests` que o gateway já grava. Envia
dois tipos de evento ao mesmo projeto do site, sem prompts, respostas, emails,
chaves, headers, IPs ou mensagens de erro. Não habilita LLM Analytics nem envia
um evento por chamada. Não muda a cobrança ou o provisionamento da Stac.

## Configuração de produção

1. Revisar e aplicar `supabase/migrations/20261002142202_posthog_gateway_analytics.sql`
   pelo processo de migrations existente. Este PR não aplicou alterações à nuvem.
2. Fazer build/deploy da imagem do gateway no host que Diogo administra. Não é
   necessário instalar SDK novo: a integração usa o `httpx` já existente.
3. Configurar o token de ingestão do projeto PostHog **633289**, região US,
   conforme `docker/gateway/.env.analytics.example`. Definir explicitamente
   `POSTHOG_GATEWAY_ENVIRONMENT=production` e `POSTHOG_PROJECT_TOKEN` nesse host.
   Não colocar o valor do token no Git ou em logs. Em desenvolvimento e preview,
   manter `disabled` e o token vazio.
4. Após o deploy aprovado, conferir no PostHog eventos reais de produção e o
   UID de uma conta conhecida. Um HTTP 2xx confirma aceitação do envio, sem
   garantir que um token incorreto gere dados no projeto esperado. A conferência
   deve usar tráfego real; os testes deste PR não enviam eventos ao PostHog.

O desligamento exige só `POSTHOG_GATEWAY_ENVIRONMENT=disabled`. O exportador não
consulta a outbox nem usa a rede enquanto desligado; novas linhas do ledger
ficam sem os campos opcionais de analytics. As correções de classificação de
stream incompleto/abortado continuam preservando o status lógico do ledger.

## Contrato dos eventos

`distinct_id` é `accounts.user_id`, o mesmo UID Supabase usado pelo site.
`account_id` é a conta dona da chave, não o ID da chave nem o ID da máquina.
Todos os eventos têm `environment=production` e `$internal_or_test_user:boolean`.
O domínio `@trystac.com` é verificado localmente; o email não sai do banco.
A mesma classificação é enviada por `$set` à pessoa, para o filtro de equipe.
O modelo vem do catálogo/máquina efetiva do gateway; um alias inválido vira
`unknown`, sem aceitar uma string arbitrária do corpo do cliente.

- `first_inference_completed`: primeiro sucesso de API de cliente observado
  após ativar esta integração, uma vez por conta. Não representa o primeiro uso
  histórico da vida da conta. Usa o horário durável da linha do ledger e inclui
  modelo, caminho, stack e tokens/duração quando conhecidos. Streaming exige
  marcador terminal válido, frame SSE completo e ausência de cancelamento,
  timeout ou erro; EOF sem o marcador não é sucesso.
- `api_usage_daily`: uma linha por conta, modelo e **dia UTC encerrado**, com
  `period_start` inclusivo e `period_end` exclusivo. O processamento espera duas
  horas após a virada UTC; nunca envia snapshots parciais do dia. O horário do
  evento é o início desse período. O dia é definido pela gravação durável no
  ledger, depois do fechamento da resposta.

O resumo contém `request_count`, `success_count`, `error_count`, `aborted_count`,
`stream_count`, `tokens_in`, `tokens_out`, `tokens_known_count`,
`duration_ms_sum`, `duration_known_count`, `duration_ms_p95`,
`reported_cost_usd_sum` e `cost_known_count`.
`request_count = success_count + error_count + aborted_count`; sucesso não exige
que o provedor informe tokens ou duração. Contagens desconhecidas não viram
consumo conhecido: consultar também os campos `*_known_count`. Latência média
entre summaries é `sum(duration_ms_sum) / sum(duration_known_count)`, nunca a
média dos percentis. O p95 descreve somente a distribuição daquele grupo/dia.
O custo é só o valor reportado pelo fornecedor no ledger, sem estimativa de GPU,
receita ou margem; zero com `cost_known_count=0` significa custo desconhecido.

Os caminhos incluídos são chat/completions, completions, responses, messages,
embeddings, documents/extract, images/extract, images/generations e images/edits.
Playground, listagem de modelos e geração de documento/PDF ficam fora deste
resumo. Rejeições antes dos pontos que já gravam no ledger (por exemplo 401,
parte dos 429 e máquina indisponível) também ficam fora: não é um monitor de
todas as requisições HTTP da plataforma. Erros do provedor depois do início da
inferência entram como falha; desconexão do cliente entra como abortada.

## Volume, entrega e banco

O orçamento local padrão é **200 mil envelopes únicos por mês**, compartilhado
entre réplicas no banco e limitado a esse máximo mesmo com configuração maior.
O volume normal é aproximadamente contas ativas × modelos × dias, mais os
primeiros usos. O PostHog gratuito tem cota global de Product Analytics; eventos
do site consomem a mesma cota. Manter o limite global de cobrança do projeto e
acompanhar o uso: este orçamento do gateway não limita outros produtores.

A outbox guarda UUID, evento, UID, timestamp e propriedades imutáveis. Envio em
lotes de até 100, lease de cinco minutos, retry sem reservar outra unidade no
mesmo mês e ack somente após HTTP 2xx. Um ack perdido pode reenviar o mesmo
envelope, que mantém todos os campos exigidos pela deduplicação do PostHog.
Isso não promete entrega exactly-once. Backlog sem orçamento retoma no mês
seguinte; falhas de entrega ficam persistidas.

O checkpoint do resumo avança na mesma transação da outbox. Uma falha no trigger
de analytics não desfaz a linha do ledger de negócio; o processamento recupera
o primeiro fato a partir do ledger. O cliente só remove campos opcionais e
repete o INSERT quando PostgREST confirma coluna de analytics ausente
(400/PGRST204), nunca após timeout, falha de rede ou erro genérico de servidor.
Nenhuma transação ou chamada ao PostHog é aguardada pela resposta de inferência.

As tabelas e quatro funções são restritas ao `service_role`, com RLS habilitado,
`SECURITY INVOKER` e `search_path` vazio. O índice parcial de data evita varrer
todo o histórico para cada janela. Summaries enviados são removidos da outbox
após 90 dias; primeiros usos permanecem para deduplicação, e itens não enviados
nunca são apagados automaticamente. Esta retenção não muda o ledger existente.
Se houver uma interrupção prolongada, acompanhar o espaço do banco/backlog.

## Verificação local

Os testes de Python usam transportes em memória, sem APIs pagas ou produção.
Cobrem privacidade, configuração desligada, cancelamento/EOF, frames terminais
fragmentados nos caminhos bruto/OpenRouter/filtro/Anthropic, status/usage,
fallback de schema e retry de entrega com envelope estável.
O teste `docker/gateway/test_product_analytics_sql.mjs` executa a migration em
PostgreSQL local via PGlite, com rollback do checkpoint, recuperação após falha
real da outbox, retenção, leases, orçamento e permissões. Esse engine usa uma
conexão e não é prova de stress entre réplicas; os locks SQL serializam a reserva
e o checkpoint. A revisão independente conferiu permissões e o trigger que
preserva o ledger sob falha da outbox. Nenhuma migration foi aplicada à nuvem.

Referências: [Capture API](https://posthog.com/docs/api/capture) e
[deduplicação de eventos](https://posthog.com/docs/data/events#event-deduplication).
