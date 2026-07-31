-- Nenhum registro de quando uma chave foi usada pela última vez — o painel
-- só tinha created_at. Tocado pelo coletor periódico de usage_metrics do
-- gateway (collect_usage_metrics_once), não por request individual: mesma
-- granularidade que já existe pra tokens/requests, sem escrita extra por
-- chamada.
alter table api_keys add column if not exists last_used_at timestamptz;
