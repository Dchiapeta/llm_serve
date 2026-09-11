"""Só METADADO correlacionado à request: por que a política decidiu o que
decidiu, e quantos trechos entraram.

Nunca o conteúdo — nem mensagem, nem documento recuperado, nem raciocínio. O
que estes logs precisam responder é "esta request pensou? consultou a base?
por quê?", e nada disso exige guardar o texto.
"""

import json
import logging
from contextvars import ContextVar

request_trace_id = ContextVar("generation_request_id", default=None)
logger = logging.getLogger("gateway.generation")


def trace_decision(kind: str, entry: dict, **metadata) -> None:
    logger.info("generation_policy %s", json.dumps({
        "request_id": request_trace_id.get(),
        "kind": kind,
        "stack_id": entry.get("stack_id"),
        "api_key_id": entry.get("api_key_id"),
        **metadata,
    }, ensure_ascii=False))
