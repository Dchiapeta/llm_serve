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


# Níveis do chat template do Qwen3.8 (low/medium/xhigh, default xhigh). O
# protocolo OpenAI fala low/medium/high, e o "high" do cliente vira o topo real
# do template.
#
# O campo top-level NÃO pode sobreviver à tradução. Medido em produção
# (13/09/2026): com `reasoning_effort: "high"` no corpo, o template respondeu
# 400 "Unexpected reasoning effort high. Supported types are xhigh (default),
# medium, and low" — o valor cru chega ao jinja e vence o que estiver em
# chat_template_kwargs. `low` e `medium` passavam por coincidência de
# vocabulário, então o bug só aparecia no `high`. Por isso apply_thinking_policy
# traduz para chat_template_kwargs E REMOVE o top-level: o on/off já está em
# enable_thinking (explícito), e o nível, no kwarg traduzido.
EFFORT_TO_TEMPLATE = {"high": "xhigh"}


@dataclass(frozen=True)
class ThinkingPolicy:
    enabled: bool | None
    source: str
    # nível pedido pelo cliente (low/medium/high); None = default do template
    effort: str | None = None


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
    effort_pedido = None
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
            if effort != "none":
                effort_pedido = effort

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

    # Nível de raciocínio da CHAVE (migration 0069): none/low/medium/high, ou
    # None para herdar. Vem antes de default_enable_thinking na precedência
    # porque é a configuração mais específica — diz o on/off E o nível.
    key_effort = entry.get("default_reasoning_effort")
    if key_effort is not None and key_effort not in ("none", "low", "medium", "high"):
        raise ThinkingPolicyError("default_reasoning_effort deve ser none, low, medium, high ou null")

    if requested:
        if any(value != requested[0] for value in requested):
            raise ThinkingPolicyError("parâmetros de thinking conflitantes na requisição")
        effort = effort_pedido if requested[0] else None
        if requested[0] and effort is None and key_effort not in (None, "none"):
            # a request só LIGOU o thinking (enable_thinking / thinking.type),
            # sem dizer o nível: o nível da chave preenche, como faria um
            # default de sampling. Um reasoning_effort explícito já veio acima.
            effort = key_effort
        policy = ThinkingPolicy(requested[0], "request", effort)
    elif key_effort is not None:
        policy = ThinkingPolicy(key_effort != "none", "key",
                                key_effort if key_effort != "none" else None)
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
    if policy.enabled and policy.effort and "reasoning_effort" not in kwargs:
        # o nível só faz sentido com thinking ligado; um reasoning_effort já
        # explícito em chat_template_kwargs é do cliente e vence
        kwargs["reasoning_effort"] = EFFORT_TO_TEMPLATE.get(policy.effort, policy.effort)
    body["chat_template_kwargs"] = kwargs
    # o top-level sai SEMPRE que a política foi materializada (ver
    # EFFORT_TO_TEMPLATE): deixá-lo aí faz o valor cru do protocolo chegar ao
    # chat template e derrubar a request com 400. `reasoning` (objeto da
    # Responses API) não é tocado — é outro campo, servido nativamente pelo vLLM.
    body.pop("reasoning_effort", None)
    # Tudo converge para a chave que o chat template realmente lê. É isto que
    # thinking_esperado_de e o filtro de <think> passam a ler depois — a
    # decisão materializada no corpo, não a política solta. `thinking` sai
    # porque é campo Anthropic e o vLLM recusaria.
    body.pop("thinking", None)
