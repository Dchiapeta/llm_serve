-- Estado de cobrança da stack: o que decide se a chave de API responde ou não.
--
-- Até aqui não existia NENHUMA noção de inadimplência no schema — a única
-- alavanca de corte era revogar `api_keys` (destrutivo: o cliente perde a
-- chave e precisa trocá-la em todas as integrações dele ao voltar a pagar).
-- Estas colunas permitem cortar e restaurar o acesso sem tocar na chave.
--
-- Quem escreve: o trigger `project_subscription_to_stack` (migration 0029 do
-- repo TryStac, dono de `chargefy_subscriptions`) e o loop de reconciliação
-- do gateway. A aplicação nunca escreve isto à mão — ver SHARED_SCHEMA.md.
--
-- Quem lê: `find_active_key` (docker/gateway/supa.py) traz as duas colunas
-- embutidas no select de stacks, e `authenticate` (docker/gateway/main.py)
-- aplica a regra por request.
--
-- ORDEM DE DEPLOY: esta migration tem que estar aplicada ANTES do gateway que
-- lê as colunas. `find_active_key` faz raise_for_status(), e um select com
-- coluna inexistente devolve 400 (42703) do PostgREST — o que viraria 500 em
-- 100% do tráfego, não uma degradação parcial.
--
-- Idempotente: pode rodar mais de uma vez.

-- `billing_status` nasce 'active' para toda stack existente. É o default
-- correto e não uma conveniência: quem já está na base é cliente pagante, e
-- qualquer outro valor cortaria a produção inteira no instante do ALTER.
alter table stacks
  add column if not exists billing_status text not null default 'active';

-- Momento em que a assinatura entrou em atraso — um FATO, não uma decisão.
-- A política de tolerância (72h) vive como constante no gateway
-- (BILLING_GRACE_HOURS), então mudá-la não exige migrar dado nenhum. Guardar
-- um `grace_until` já calculado teria o efeito oposto: a política ficaria
-- congelada em cada linha, e mudá-la exigiria reescrever a tabela.
--
-- NULL = não está em atraso. Sair do atraso zera a coluna, então não existe
-- a pergunta "quem reseta o relógio" — ele é apagado junto com o estado.
alter table stacks
  add column if not exists past_due_since timestamptz;

-- Chave de idempotência do provisionamento via checkout: recebe o
-- `client_reference` de chargefy_checkout_attempts. É o ponto de
-- serialização de POST /api/stacks — a Chargefy entrega webhooks
-- at-least-once (até 9 tentativas em ~3 dias), e sem um unique aqui duas
-- entregas concorrentes criariam duas stacks para o mesmo pagamento.
--
-- Nullable porque stack criada pelo painel admin não vem de checkout nenhum;
-- o índice é parcial pela mesma razão (vários NULLs convivendo).
alter table stacks
  add column if not exists provisioning_ref uuid;

create unique index if not exists stacks_provisioning_ref_uidx
  on stacks (provisioning_ref)
  where provisioning_ref is not null;

-- CHECK nomeado e adicionado à parte (o `add column` acima não aceita
-- `if not exists` para constraint) — sem o guard, rodar a migration duas
-- vezes falharia com 42710.
--
-- 'suspended' é o estado terminal de corte; 'past_due' ainda tem acesso, e é
-- o par (past_due, past_due_since) que o gateway avalia contra as 72h.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'stacks_billing_status_valid'
  ) then
    alter table stacks
      add constraint stacks_billing_status_valid
      check (billing_status in ('active', 'trialing', 'past_due', 'suspended', 'canceled'));
  end if;
end $$;

-- Consultado pelo loop de reconciliação a cada 60s (billing_reconcile_once em
-- docker/gateway/main.py), que procura exatamente as linhas em atraso com
-- prazo vencido. Parcial: o estado interessante é raro perto do total.
create index if not exists stacks_past_due_idx
  on stacks (past_due_since)
  where billing_status = 'past_due';
