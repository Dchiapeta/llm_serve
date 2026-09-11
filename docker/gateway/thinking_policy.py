"""Decide o thinking UMA vez, desacoplado do teto de saída.

Até aqui "raciocinar" e "quanto pode gerar" eram a mesma decisão: max_tokens
abaixo de MIN_MAX_TOKENS desligava o thinking (ver o comentário do piso em
main.py). Isso resolvia o caso do cliente que pede pouco de propósito, mas
impedia as duas combinações legítimas — saída longa sem raciocínio e resposta
curta com raciocínio.

`enabled is None` é POLÍTICA LEGADA, não thinking desligado: é o que preserva
o comportamento antigo nas stacks ainda não migradas. Sem rede e sem env: a
decisão é pura, e quem materializa no corpo é apply_thinking_policy.
"""

import re
from dataclasses import dataclass


class ThinkingPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ThinkingPolicy:
    enabled: bool | None
    source: str


def supports_thinking_switch(machine: dict) -> bool:
    # O checkpoint, nunca o "model" do cliente nem o alias comercial: os dois
    # são texto escolhido por quem chama e não dizem nada sobre o chat template.
    model = (machine.get("model_name") or "").lower()
    # Qualquer minor da linha Qwen3 híbrida: 3.5 (Go), 3.6 e 3.8 (Pro) já estão
    # em uso, e fixar as minors conhecidas fazia o Pro (Qwen/Qwen3.8-27B-FP8)
    # cair no ramo "não suportado" — uma stack configurada lá viraria erro em
    # toda request. As exclusões abaixo é que são a lista real: Coder,
    # -Thinking e a série 2507 vêm em checkpoints separados por modo, sem
    # switch no chat template.
    return bool(
        re.search(r"(?:^|/)qwen3(?:\.\d+)?-", model)
        and "coder" not in model
        and "thinking" not in model
        and "2507" not in model
    )


def resolve_thinking_policy(body: dict, entry: dict, stack: dict | None,
                            machine: dict) -> ThinkingPolicy:
    requested = []
    kwargs = body.get("chat_template_kwargs")
    if kwargs is not None and not isinstance(kwargs, dict):
        raise ThinkingPolicyError("chat_template_kwargs deve ser um objeto")
    if isinstance(kwargs, dict) and "enable_thinking" in kwargs:
        enabled = kwargs["enable_thinking"]
        if not isinstance(enabled, bool):
            raise ThinkingPolicyError("enable_thinking deve ser booleano")
        requested.append(enabled)

    reasoning = body.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise ThinkingPolicyError("reasoning deve ser um objeto")
    for effort in (body.get("reasoning_effort"), (reasoning or {}).get("effort")):
        if effort is not None:
            if effort not in ("none", "low", "medium", "high"):
                raise ThinkingPolicyError("reasoning effort suportado: none, low, medium ou high")
            requested.append(effort != "none")

    # anthropic_compat repassa este campo intacto para a validação em vez de
    # tratá-lo na conversão: assim o erro sai pelo tratamento HTTP que o
    # protocolo já tem (e que libera o flight), em vez de virar 500.
    thinking = body.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, dict) or thinking.get("type") not in ("enabled", "disabled"):
            raise ThinkingPolicyError("thinking.type suportado: enabled ou disabled")
        if "budget_tokens" in thinking:
            raise ThinkingPolicyError("thinking.budget_tokens não é suportado por este endpoint")
        requested.append(thinking["type"] == "enabled")

    if requested:
        if any(value != requested[0] for value in requested):
            raise ThinkingPolicyError("parâmetros de thinking conflitantes na requisição")
        policy = ThinkingPolicy(requested[0], "request")
    else:
        policy = ThinkingPolicy(None, "legacy")
        for source, config in (("key", entry), ("stack", stack or {})):
            value = config.get("default_enable_thinking")
            if value is not None:
                if not isinstance(value, bool):
                    raise ThinkingPolicyError("default_enable_thinking deve ser booleano ou null")
                policy = ThinkingPolicy(value, source)
                break
    if policy.enabled is not None and not supports_thinking_switch(machine):
        raise ThinkingPolicyError("alternância de thinking não suportada para este modelo")
    return policy


def apply_thinking_policy(body: dict, policy: ThinkingPolicy) -> None:
    if policy.enabled is None:
        return
    kwargs = dict(body.get("chat_template_kwargs") or {})
    kwargs["enable_thinking"] = policy.enabled
    body["chat_template_kwargs"] = kwargs
    # Tudo converge para a chave que o chat template realmente lê. É isto que
    # thinking_esperado_de e o filtro de <think> passam a ler depois — a
    # decisão materializada no corpo, não a política solta. `thinking` sai
    # porque é campo Anthropic e o vLLM recusaria; reasoning_effort fica, que
    # há modelo que usa os dois.
    body.pop("thinking", None)
