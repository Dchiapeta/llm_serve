"""Quem consulta a base de conhecimento, e o que pula a busca.

Duas decisões independentes moram aqui, nesta ordem:

  1. `resolve_rag_policy` — SE esta chave consulta a base (migration 0065).
  2. `is_isolated_greeting` — porta conservadora que dispensa a busca para
     uma saudação isolada, depois que (1) já disse que sim.

Módulo puro (sem rede e sem env), como thinking_policy.py: quem faz a busca e
monta o bloco de contexto é retrieve_stack_context em main.py.
"""

import re
import unicodedata
from dataclasses import dataclass

GREETINGS = {"opa", "oi", "ola", "bom dia", "boa tarde", "boa noite", "hello", "hi"}


@dataclass(frozen=True)
class RagPolicy:
    enabled: bool | None
    source: str


def resolve_rag_policy(entry: dict) -> RagPolicy:
    """A chave consulta a base de conhecimento? `entry` é a linha de api_keys.

    `enabled is None` é POLÍTICA LEGADA, não "desligado": é o comportamento
    antigo — consulta só quando a request NÃO traz `system`/`instructions` —
    preservado para as chaves ainda não migradas. `False` é o desligamento
    explícito, e os dois ramos precisam ser distinguíveis no call site.

    SEM EIXO DE REQUEST, ao contrário de resolve_thinking_policy, que começa
    lendo o corpo. Não existe parâmetro de protocolo que signifique "consulte
    a base": nem OpenAI nem Anthropic têm campo para isso, e inventar um
    obrigaria o cliente a mandá-lo em toda request — exatamente o que a
    configuração por chave existe para evitar. O que havia era o `system` do
    cliente ocupando esse papel POR ACIDENTE (mandou instrução ⇒ não consulta),
    e é esse acidente que esta função desfaz: a presença de `system` volta a
    significar só "o cliente tem instrução própria".

    Valor não-booleano degrada para legado em vez de virar 400. A assimetria
    com o thinking é deliberada: a política de thinking é MATERIALIZADA no
    corpo que vai pro vLLM (apply_thinking_policy), então um valor ruim tem
    que parar a request antes de virar uma geração errada e cobrada. O RAG é
    best-effort por contrato — ver embed_query em main.py, que engole toda
    exceção e devolve None — e derrubar a inferência inteira por causa de uma
    coluna com lixo (só alcançável por escrita direta no banco) seria uma
    degradação pior do que responder sem contexto."""
    value = (entry or {}).get("enable_knowledge_base")
    return RagPolicy(value, "key") if isinstance(value, bool) else RagPolicy(None, "legacy")


def is_isolated_greeting(text: str) -> bool:
    """Curto não é critério — "Certificações?" é pergunta legítima sobre a
    base, e "opa, quais são minhas certificações?" também. Por isso a
    comparação é com o texto inteiro normalizado, nunca com um prefixo ou com
    o tamanho."""
    normalized = "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                         if not unicodedata.combining(c))
    normalized = re.sub(r"[.!?,;:¡¿]+", " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized in GREETINGS
