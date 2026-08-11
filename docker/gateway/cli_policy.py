"""Quais PLANOS podem usar a stack para codar — o corte de CLI por plano.

O Go deixou de servir para codar. Um cliente Go usa a Stac por requests de API
(chat, imagem, extração de documento); CLI e assistente de código (Claude Code,
Codex, Cursor, Cline, Roo, Continue) passaram a ser do Pro para cima.

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

A camada 2 existe porque sem ela metade das ferramentas de código passaria
batido; ela não existe pra ser inescapável. Tem exatamente a mesma força do UA
em lib/request-origin.ts: acionável, jamais controle de segurança. Quem sustenta
o contrato comercial é o plano na fatura, não o header.

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
# justamente o uso que o Go continua tendo. Importar de lá em vez de repetir os
# padrões de UA é de propósito — a lista já vive em dois lugares (Python + TS) e
# um terceiro seria o que garante que eles divirjam.
CODING_TOOLS = frozenset({"claude-code", "codex", "cursor", "cline", "roo", "continue"})

# Planos sem CLI. "VibeCoder" é o nome antigo de "Go": entrada mantida como nos
# outros dicts por plano do gateway (RATE_LIMIT_RPM, DAILY_TOKEN_BUDGET,
# MAX_CLIENTS_BY_PLAN) até a migration 0049 rodar em produção.
CLI_BLOCKED_PLANS = frozenset({"Go", "VibeCoder"})

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
    return (
        f"o plano {plan} não inclui uso via CLI ou assistente de código — "
        "faça upgrade para o Pro em app.trystac.com"
    )
