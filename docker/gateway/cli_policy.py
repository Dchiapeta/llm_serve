"""Quais PLANOS podem usar a stack para codar — o corte de CLI por plano.

Desde 16/10/2026 todos os planos de LLM (Go, Pro, Max, Enterprise) têm acesso à
CLI e a assistentes de código (Claude Code, Codex, Cursor, Cline, Roo,
Continue). O que separa Go de Pro é a cota mensal de requisições, ponderada
pelo tamanho da entrada (request_quota.py, migration 0079), e os demais limites
por plano (rate limit, ambientes, extração, catálogo de modelos) — não a
ferramenta. O Go esteve sem CLI de 11/08/2026 a 16/10/2026, quando a requisição
contava 1 independentemente do tamanho; com a cota ponderada, uma requisição
de agente de código paga pelo contexto que usa, e o corte deixou de ter razão
comercial.

O único plano barrado é "Image": não é uma decisão comercial, é que um pod de
difusão não fala /v1/chat/completions. Apontar Claude Code ou Cursor para uma
stack de imagem daria erro em toda requisição — o 403 daqui pelo menos explica
por quê. O ramo existe só para entradas legadas (stacks ainda cacheadas como
plan=Image durante a migration 0060); as stacks atuais de imagem são
plan=Go/category=image e são barradas antes, pelo guard de categoria em
authenticate.

## Por que duas camadas, com forças diferentes

O produto é BYOE — o cliente aponta a ferramenta dele para o nosso endpoint —
então "qual ferramenta" não é um dado que a gente controle. Sobram dois sinais,
e eles têm garantias muito diferentes:

  1. PATH (dura). /v1/messages e /v1/responses são protocolos que só o Claude
     Code e o Codex falam (ver anthropic_compat.py e ALLOWED_V1 em main.py).
     Quem quiser escapar daqui não tem header pra forjar: teria que reescrever
     o cliente pra falar chat/completions, que é deixar de ser aquele cliente.
  2. USER-AGENT (best-effort). Cursor, Cline, Roo e Continue falam a MESMA rota
     chat/completions de um SDK qualquer, então só o UA os distingue — e o UA é
     string do cliente, forjável com uma variável de ambiente.

A camada 2 tem exatamente a mesma força do UA em lib/request-origin.ts:
acionável, jamais controle de segurança. Quem sustenta o contrato comercial é
o plano na fatura, não o header.

## Onde erra, e para que lado

Erra para o lado de DEIXAR PASSAR, sempre:

  - UA ausente ou desconhecido → passa. Um cliente novo de código não é pego
    até entrar na lista; em troca, nenhum SDK legítimo com UA exótico leva 403.
  - Plano fora de CLI_BLOCKED_PLANS → passa (mesmo fail-open de
    usage_class.high_cap e client_identity.client_cap): criar um plano novo e
    esquecer deste set libera CLI onde talvez não devesse, o inverso barraria
    cliente pagante por omissão.

Módulo puro (sem I/O), como usage_class.py e client_identity.py: a decisão é uma
função dos três sinais, e o I/O de resolver plano/stack fica em main.py, junto
de billing_blocked e enforce_client_limit.
"""

import os

from client_identity import normalize_tool

# Rotas que SÓ um cliente de código fala. Os nomes são os mesmos que
# log_gateway_request grava em gateway_requests.path (migration 0038) e que
# BY_PATH consome em lib/request-origin.ts — divergir aqui faria o corte e o
# histórico do painel discordarem sobre o que é uma requisição de CLI.
CLI_ONLY_PATHS = frozenset({"messages", "messages/count_tokens", "responses"})

# Slugs de normalize_tool (client_identity.py) que são ferramenta de CÓDIGO.
# Deliberadamente NÃO é a lista inteira de TOOL_PATTERNS: "sdk" e "http" são
# uso de API, não de assistente. Importar de lá em vez de repetir os padrões
# de UA é de propósito — a lista já vive em dois lugares (Python + TS) e um
# terceiro seria o que garante que eles divirjam.
CODING_TOOLS = frozenset({"claude-code", "codex", "cursor", "cline", "roo", "continue"})

# Planos sem CLI. Só "Image" (ver docstring do módulo). Espelha
# CLI_BLOCKED_PLANS de playground-limits.ts no TryStac, que é quem impede o
# painel de ENSINAR essa configuração.
CLI_BLOCKED_PLANS = frozenset({"Image"})

# Corte x observação. Sobe LIGADO — diferente de CLIENT_LIMIT_ENFORCE, aqui não
# há fingerprint a calibrar: o corte por path é exato, e o corte por UA erra só
# para o lado de deixar passar. A variável existe como escape hatch pra desligar
# sem redeploy se algum cliente legítimo aparecer barrado.
CLI_POLICY_ENFORCE = os.environ.get("CLI_POLICY_ENFORCE", "1") not in ("", "0", "false")


def cli_block_reason(plan: str | None, path: str | None, user_agent: str | None) -> str | None:
    """Motivo do corte por CLI, ou None se a requisição pode passar.

    Só decide; não levanta e não loga — main.py faz as duas coisas, porque é
    lá que fica a diferença entre corte e observação."""
    if plan not in CLI_BLOCKED_PLANS:
        return None

    if path in CLI_ONLY_PATHS:
        return _detail(plan)

    slug, _label = normalize_tool(user_agent)
    if slug in CODING_TOOLS:
        return _detail(plan)

    return None


def _detail(plan: str | None) -> str:
    """Mensagem ao cliente. Mesmo tom de _client_limit_detail (main.py): diz o
    que aconteceu e o que fazer a respeito, sem culpar a credencial."""
    if plan == "Image":
        return (
            "o plano Image serve geração de imagem (/v1/images/*), não conversa "
            "nem código — uma CLI ou assistente de código precisa de uma stack "
            "de um plano de LLM (Go, Pro ou Max)"
        )
    return (
        f"o plano {plan} não tem acesso à CLI nem a assistentes de código — "
        "para liberar o acesso, troque para um plano de LLM em app.trystac.com"
    )
