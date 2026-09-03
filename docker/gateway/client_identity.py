"""Quantos LUGARES distintos usam uma stack — o detector de chave compartilhada.

O plano já limitava capacidade (slots de máquina), vazão (RATE_LIMIT_RPM) e
volume (DAILY_TOKEN_BUDGET), mas nada impedia colar a mesma chave em 20
máquinas: uma assinatura Go valia por uma equipe inteira.

São DUAS camadas, e esta é a mais fraca:

  1. Chaves ativas por stack (MAX_KEYS_BY_PLAN, lib/types.ts) — o CONTRATO.
     Exato, sem adivinhação, aplicado na emissão da chave pelo painel.
  2. Ambientes distintos (este módulo) — o DETECTOR de quem furou o contrato
     usando uma única chave em todo canto.

## Por que o fingerprint é aproximado

Numa API não há sessão nem cookie, e nenhum cliente do produto (Claude Code,
Cursor, Codex) manda identificador de máquina. Sobram dois sinais no request:

  - User-Agent -> identifica a FERRAMENTA, nunca a máquina. Dois devs rodando
    Claude Code mandam a mesma string.
  - IP de origem -> identifica a REDE. Vem de CF-Connecting-IP (o gateway fica
    atrás do Cloudflare), com X-Forwarded-For de reserva.

O fingerprint é o par. O que ele acerta: revenda/compartilhamento entre redes
diferentes, que é o abuso que custa caro. O que ele erra, nas duas direções:
um dev que trabalha de casa, do escritório e do celular conta como 3; dez
pessoas atrás do mesmo NAT contam como 1.

## Limitação conhecida (não é bug, é o teto da abordagem)

Os dois sinais são headers controlados pelo cliente. Quem bater no domínio do
Railway direto, sem passar pelo Cloudflare, forja o bloco de rede à vontade.
Este limite tem exatamente a mesma força do User-Agent em
lib/request-origin.ts: telemetria acionável, jamais controle de segurança.
Quem sustenta o contrato comercial é a camada 1.

## Bloco de rede, não IP

Persistimos /24 (IPv4) ou /48 (IPv6), nunca o endereço completo. Dois motivos,
os dois necessários: IP residencial muda quando o ISP renova o lease, e contar
IP exato faria o mesmo dev estourar o limite sozinho em uma semana; e IP cheio
é dado pessoal identificável, enquanto o bloco é bem mais defensável.

Módulo puro (sem I/O) de propósito, como usage_class.py: a decisão de admissão
mora na RPC touch_stack_client (migration 0051) — estado autoritativo no
banco, não em memória de réplica única como rate_buckets/in_flight — e o
I/O fica em main.py, junto dos outros caches.
"""

import hashlib
import ipaddress
import os

# Muda junto com a FÓRMULA do fingerprint, nunca com a lista de ferramentas.
# Bumpar isto invalida todos os ambientes conhecidos de uma vez: cada um vira
# "novo" e disputa vaga do zero, o que num plano lotado significa 403 para
# quem já estava dentro. Só bumpe junto de uma janela de observação.
FINGERPRINT_VERSION = "v1"

# Espelho de BY_USER_AGENT em lib/request-origin.ts (sem a parte de cores, que
# só a UI usa) — manter os dois em sincronia. Ordem importa: primeiro match
# vence, padrões mais específicos primeiro, porque um cliente pode mandar
# "cursor" E "openai-node" no mesmo UA.
TOOL_PATTERNS: list[tuple[tuple[str, ...], str, str]] = [
    (("claude-cli", "claude-code", "claudecode"), "claude-code", "Claude Code"),
    (("codex",), "codex", "Codex"),
    (("cursor",), "cursor", "Cursor"),
    (("cline",), "cline", "Cline"),
    (("roo",), "roo", "Roo"),
    (("continue",), "continue", "Continue"),
    # Automação, não ferramenta de código: fica FORA de CODING_TOOLS
    # (cli_policy.py) de propósito, porque workflow de n8n é justamente o uso
    # que o plano Go mantém. Antes de "sdk"/"http" porque o n8n manda hoje o UA
    # literal "n8n", mas usa axios por baixo e pode voltar a citá-lo.
    (("n8n",), "n8n", "n8n"),
    (
        ("openai-python", "openai-node", "anthropic-sdk", "anthropic-python", "langchain"),
        "sdk",
        "SDK",
    ),
    (
        ("curl", "httpie", "python-requests", "postman", "insomnia", "node-fetch", "axios"),
        "http",
        "HTTP",
    ),
]

# UA presente mas fora da lista. Deliberadamente NÃO derivamos o slug da string
# crua: a versão vem embutida no UA ("claude-cli/2.1.4"), e uma ferramenta
# desconhecida que se atualiza viraria um ambiente novo a cada release do
# cliente. Colapsar tudo em "other" erra para o lado de contar de MENOS, que é
# o lado certo de errar aqui.
UNKNOWN_TOOL = ("other", "Outro")
MISSING_TOOL = ("unknown", "Sem identificação")

# Rede desconhecida (nenhum header de IP: chamada local, teste, proxy que não
# repassa). Todos os requests sem rede colapsam num ambiente por ferramenta —
# de novo, contar de menos em vez de bloquear à toa.
UNKNOWN_BUCKET = "unknown"

IPV4_PREFIX = 24
IPV6_PREFIX = 48


def _cap_from_env(name: str, default: int | None) -> int | None:
    """Teto por plano, sobrescrevível por env pra calibrar sem deploy.

    0 (ou negativo) = SEM TETO, mesma convenção de DAILY_TOKEN_BUDGET. Valor
    inválido cai no default em vez de levantar: o módulo é importado no boot
    do gateway, e uma env mal digitada não pode virar outage de todo o
    tráfego (mesmo raciocínio de BILLING_GRACE_HOURS em main.py)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


# Teto de ambientes simultâneos por stack. Espelha MAX_CLIENTS_BY_PLAN em
# lib/types.ts, que é o que o painel exibe — divergir faz a UI mentir sobre o
# número que o gateway aplica.
#
# "VibeCoder" é o nome antigo de "Go": entrada mantida como nos outros dicts
# por plano deste gateway, até a migration 0049 rodar em produção.
# Enterprise = None (sem teto), mesma convenção de RAG_FILE_LIMIT_BY_PLAN.
MAX_CLIENTS_BY_PLAN: dict[str, int | None] = {
    "Go": _cap_from_env("MAX_CLIENTS_GO", 5),
    "VibeCoder": _cap_from_env("MAX_CLIENTS_GO", 5),
    "Pro": _cap_from_env("MAX_CLIENTS_PRO", 25),
    "Max": _cap_from_env("MAX_CLIENTS_MAX", 50),
    "Enterprise": _cap_from_env("MAX_CLIENTS_ENTERPRISE", None),
    # PLACEHOLDER, espelhando lib/types.ts: o plano Image ainda não tem preço
    # definido. O número precisa existir aqui de qualquer forma — sem a chave, o
    # gateway aplicaria o default (5 do "Go") e o painel exibiria outro valor.
    "Image": _cap_from_env("MAX_CLIENTS_IMAGE", 5),
}

# Vaga é por janela deslizante: ambiente parado libera sozinho. Sem isso,
# trocar de notebook viraria ticket de suporte.
CLIENT_WINDOW_DAYS = int(os.environ.get("CLIENT_WINDOW_DAYS", "14"))

# Intervalo mínimo entre dois toques do MESMO ambiente. Mesmo papel do
# TOUCH_THROTTLE_S de maybe_touch, com folga muito maior: last_seen_at só
# alimenta uma janela de 14 dias, então 5 min de granularidade sobra e mantém
# o caminho quente em zero I/O.
CLIENT_TOUCH_THROTTLE_S = float(os.environ.get("CLIENT_TOUCH_THROTTLE_S", "300"))

# Modo observação (default) x corte. Em observação o gateway registra o
# ambiente e loga WARNING quando estouraria o teto, mas deixa passar — é o que
# permite medir a distribuição real de ambientes por plano antes de cortar
# cliente pagante com um fingerprint ainda não calibrado.
CLIENT_LIMIT_ENFORCE = os.environ.get("CLIENT_LIMIT_ENFORCE", "0") not in ("", "0", "false")


def normalize_tool(user_agent: str | None) -> tuple[str, str]:
    """(slug, rótulo) da ferramenta a partir do User-Agent."""
    if not user_agent or not user_agent.strip():
        return MISSING_TOOL
    ua = user_agent.lower()
    for needles, slug, label in TOOL_PATTERNS:
        if any(needle in ua for needle in needles):
            return slug, label
    return UNKNOWN_TOOL


def client_ip(headers) -> str | None:
    """IP de origem, na ordem de confiabilidade.

    CF-Connecting-IP primeiro: o Cloudflare REESCREVE esse header na borda, e
    é o único dos três que um cliente não consegue plantar enquanto o tráfego
    passar por lá. X-Forwarded-For é a reserva para deploy sem Cloudflare na
    frente — primeiro elemento da cadeia, que é o cliente original."""
    for name in ("cf-connecting-ip", "x-real-ip"):
        value = headers.get(name)
        if value and value.strip():
            return value.strip()
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return None


def network_bucket(headers) -> str | None:
    """Bloco de rede do cliente ("189.45.12.0/24"), ou None se não der pra
    determinar. Nunca devolve o endereço completo."""
    raw = client_ip(headers)
    if not raw:
        return None
    # alguns proxies escrevem "[2001:db8::1]:443" ou "203.0.113.7:52310"
    if raw.startswith("["):
        raw = raw[1:].split("]", 1)[0]
    elif raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    prefix = IPV4_PREFIX if addr.version == 4 else IPV6_PREFIX
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def client_fingerprint(headers) -> tuple[str, str, str, str | None]:
    """(fingerprint, rótulo da ferramenta, user-agent cru, bloco de rede).

    O fingerprint é hash e não texto claro por privacidade: quem tiver acesso
    de leitura à tabela vê "Claude Code numa rede" sem conseguir reconstruir
    de qual rede, a não ser pelo ip_bucket que gravamos à parte para o dono da
    stack conseguir reconhecer o próprio ambiente."""
    user_agent = headers.get("user-agent") or ""
    slug, label = normalize_tool(user_agent)
    bucket = network_bucket(headers)
    material = f"{FINGERPRINT_VERSION}|{slug}|{bucket or UNKNOWN_BUCKET}"
    return hashlib.sha256(material.encode()).hexdigest(), label, user_agent, bucket


def client_cap(plan: str | None) -> int | None:
    """Teto de ambientes do plano. None = sem teto.

    Plano fora do dict cai em None (fail-open), pelo mesmo motivo de
    usage_class.high_cap: o pior caso de fail-open é um cliente com ambientes
    demais, o de fail-closed seria cliente pagante sem conseguir conectar
    porque alguém criou um plano novo e esqueceu deste dict."""
    return MAX_CLIENTS_BY_PLAN.get(plan or "")
