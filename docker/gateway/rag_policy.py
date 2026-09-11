"""Porta conservadora para saudação ISOLADA: só ela pula a busca.

Curto não é critério — "Certificações?" é pergunta legítima sobre a base, e
"opa, quais são minhas certificações?" também. Por isso a comparação é com o
texto inteiro normalizado, nunca com um prefixo ou com o tamanho.
"""

import re
import unicodedata

GREETINGS = {"opa", "oi", "ola", "bom dia", "boa tarde", "boa noite", "hello", "hi"}


def is_isolated_greeting(text: str) -> bool:
    normalized = "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                         if not unicodedata.combining(c))
    normalized = re.sub(r"[.!?,;:¡¿]+", " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized in GREETINGS
