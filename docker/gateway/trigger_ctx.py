"""Quem originou uma decisão do ciclo de vida de máquina (criar, religar,
recriar, pausar, mover stack) — e por qual caminho.

Irmão de generation_trace.py, com a mesma disciplina: só METADADO, nunca
conteúdo. O que este envelope precisa responder é "qual chave/conta/stack fez
esta máquina nascer, vindo de onde, por qual ramo da cascata" — e nada disso
exige guardar prompt, corpo ou a chave em claro. A allowlist é fechada de
propósito: uma chave nova só entra aqui, com o porquê escrito.

Mecânica: `authenticate` (main.py) seta o contexto uma vez por request. Cada
request HTTP roda na própria Task e, portanto, no próprio contexto — não há
`reset`, e não há vazamento entre requests. As tasks fire-and-forget
(recovery.spawn_tracked → asyncio.create_task) copiam o contexto no instante
da criação, então o envelope chega vivo dentro de _provision_and_track mesmo
depois de o request já ter respondido 503.

MAS a cópia é rasa — o dict é compartilhado entre pai e filho. Por isso quem
grava lê o contexto no ponto de decisão, materializa um `snapshot()` (dict
novo, filtrado) e passa o snapshot por parâmetro. Nunca mutar o dict do ctx.

Nos loops de background (lifecycle) o contexto nunca foi setado:
`snapshot()` devolve actor="lifecycle" sem nenhum código extra.
"""

from contextvars import ContextVar

from generation_trace import request_trace_id

trigger_ctx: ContextVar[dict | None] = ContextVar("gateway_trigger", default=None)

# Chaves que podem sair do gateway rumo ao banco (machine_events.trigger_meta
# e provision_decisions.trigger_meta). O painel filtra pela mesma lista ao
# receber o envelope em /api/machines/provision (lib/machine-events.ts).
ALLOWED = frozenset({
    "actor", "cause", "trace_id",
    "account_id", "account_name",
    "api_key_id", "key_prefix",
    "stack_id", "stack_slug",
    "plan", "category", "purpose",
    "path", "user_agent", "reason",
    "admin_email",
    # alvo da decisão quando já existe máquina (wake/recreate/stop)
    "machine_id", "machine_name",
})

# Mesmo teto de gateway_requests.user_agent (MAX_USER_AGENT_CHARS em main.py).
MAX_USER_AGENT_CHARS = 200

ACTOR_REQUEST = "request"
ACTOR_LIFECYCLE = "lifecycle"


def set_request_trigger(
    entry: dict, *, plan: str | None, category: str | None,
    path: str | None, user_agent: str | None,
) -> None:
    """Registra o originador da request corrente. `entry` é a chave resolvida
    por authenticate — só os identificadores saem daqui, nunca key_hash."""
    stack = None
    stack_id = entry.get("stack_id")
    if stack_id:
        stack = next(
            (s for s in entry.get("stacks") or [] if s.get("id") == stack_id), None
        )
    ua = (user_agent or "")[:MAX_USER_AGENT_CHARS] or None
    trigger_ctx.set({
        "actor": ACTOR_REQUEST,
        "account_id": entry.get("account_id"),
        "account_name": entry.get("account_name"),
        "api_key_id": entry.get("api_key_id"),
        "key_prefix": entry.get("key_prefix"),
        "stack_id": stack_id,
        "stack_slug": (stack or {}).get("slug"),
        "plan": plan,
        "category": category,
        "purpose": entry.get("purpose"),
        "path": path,
        "user_agent": ua,
    })


def snapshot(cause: str, **extra) -> dict:
    """Envelope imutável pra gravar: contexto da request (se houver) + extras
    do ponto de decisão (plan, category, reason…), filtrados pela allowlist.
    Dict NOVO — seguro pra passar a uma task de background e pra serializar.

    Sem contexto de request o ator é o lifecycle: é o que distingue "um
    cliente pediu" de "o cron do pool decidiu" na mesma coluna."""
    base = trigger_ctx.get() or {}
    merged = {**base, **extra}
    out: dict = {}
    for key, value in merged.items():
        if key in ALLOWED and value is not None:
            out[key] = value
    out["actor"] = out.get("actor") or ACTOR_LIFECYCLE
    out["cause"] = cause
    trace_id = request_trace_id.get()
    if trace_id and "trace_id" not in out:
        out["trace_id"] = trace_id
    return out
