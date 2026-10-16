"""Go e Pro com as mesmas features (decisão de 15/10/2026).

A partir da unificação, o que separa Go de Pro é só a cota mensal de
requisições (stack_request_quota, migrations 0076/0077/0079). CLI e assistentes
de código, teto de contexto, rate limit, ambientes simultâneos, limites de
extração/geração de documento e os MODELOS do OpenRouter (catálogo e reserva de
openrouter_models.plans, via main.catalog_plan) passam a ser os do Pro nos dois
planos.

Fora daqui: o pool de máquinas próprias continua separado por plano
(product_pool_key) — com machines_enabled ligado, uma stack Go segue servida
pelo template do Go.

## Por que uma flag, e não trocar os números direto

Sem a cota cortando (REQUEST_QUOTA_ENFORCE), liberar o Go seria entregar
código com contexto de 256K sem teto nenhum de uso — exatamente o que dá
prejuízo por request. A flag deixa este código subir junto com a contagem
ponderada em modo log e ser ligada no MESMO deploy de configuração que liga o
corte da cota. Desligada (o default), todo limite fica como antes.

## Como

Em vez de reescrever cada dict por plano (RATE_LIMIT_RPM, PLAN_CONTEXT_TOKENS,
MAX_CLIENTS_BY_PLAN, MAX_DOCUMENT_BYTES...), quem consulta um limite de
feature passa o plano por `feature_plan` — o Go "lê" a linha do Pro. Os dicts
continuam com os números originais, então desligar a flag volta tudo sem
redeploy de código.

Só a categoria LLM é unificada. Uma stack de imagem também é plan=Go
(category=image, migration 0060) e é outro produto, com preço e capacidade
próprios: ela continua lendo a linha do Go. As rotas de LLM já barram stack de
imagem em authenticate, então só quem é consultado nas duas categorias (o teto
de ambientes, client_identity.client_cap) precisa passar a categoria.

Módulo puro, como plan_limits.py e cli_policy.py.
"""

import os

GO_PRO_UNIFIED = (os.environ.get("GO_PRO_UNIFIED") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

# "VibeCoder" é o nome antigo de "Go", mantido como nos outros dicts por plano
# do gateway até a migration 0049 rodar em produção.
UNIFIED_PLANS = frozenset({"Go", "VibeCoder"})
UNIFIED_AS = "Pro"
LLM_CATEGORY = "llm"


def feature_plan(plan: str | None, category: str | None = LLM_CATEGORY) -> str | None:
    """Plano cujos limites de feature valem para a stack.

    Com a flag ligada, Go/VibeCoder de LLM viram "Pro"; qualquer outra
    combinação (flag desligada, Pro, Max, imagem, plano desconhecido) devolve o
    próprio plano. Mensagens ao cliente continuam usando o plano de verdade —
    isto é só para LER limites."""
    if GO_PRO_UNIFIED and plan in UNIFIED_PLANS and (category or LLM_CATEGORY) == LLM_CATEGORY:
        return UNIFIED_AS
    return plan
