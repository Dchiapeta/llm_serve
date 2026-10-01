"""Teto de janela de contexto por plano (01/10/2026).

O que o site do TryStac anuncia na linha "Contexto máximo": Go 32K, Pro 256K.
Até aqui o gateway só conhecia a janela da MÁQUINA (machines.max_model_len),
e ela não tem nada a ver com o plano: o Go roda numa máquina de 64K, o Pro numa
de 128K. Daqui sai a janela que vale para a chave, nos dois caminhos:

  máquina      janela = min(máquina, plano). Quando o PLANO é o menor dos dois
               (Go: 32K numa máquina de 64K), estourar é estourar o plano, e a
               conversa NÃO é desviada para o OpenRouter — lá ela também não
               cabe no plano. Quando a máquina é o menor (Pro: máquina de 128K,
               plano de 256K), o desvio continua: é assim que o Pro chega aos
               256K.
  OpenRouter   o próprio teto do plano, com a mesma conta de admissão do
               context_budget (sem tokenizer aqui: estimativa heurística com a
               margem de "não deu para contar").

Max e Enterprise ficam sem teto (personalizado no site): vale só a janela da
máquina, como antes. Teto <= 0 na env também desliga.

A janela é o total da request, entrada + saída, como o --max-model-len do vLLM
e a "context window" dos provedores — o mesmo número que o context_budget já
usa para clampar max_tokens.

Módulo puro, como cli_policy.py: o I/O de resolver o plano fica em main.py.
"""

import os

PLAN_CONTEXT_TOKENS: dict[str, int] = {
    "Go": int(os.environ.get("PLAN_CONTEXT_TOKENS_GO", "32768")),
    "Pro": int(os.environ.get("PLAN_CONTEXT_TOKENS_PRO", "262144")),
}


def context_cap(plan: str | None) -> int | None:
    """Teto de janela do plano, ou None se o plano não tem teto próprio."""
    cap = PLAN_CONTEXT_TOKENS.get(plan or "")
    return cap if isinstance(cap, int) and cap > 0 else None


def capped_machine(machine: dict, plan: str | None) -> tuple[dict, bool]:
    """(máquina com a janela efetiva, o teto do plano é quem limita).

    Devolve uma CÓPIA com max_model_len trocado quando o plano é menor que a
    máquina — o resto do pipeline (context_budget, count_tokens, mensagem de
    estouro) lê só esse campo e passa a respeitar o plano sem saber dele.
    Máquina sem janela conhecida (template sem --max-model-len) também recebe
    o teto: sem número nenhum o context_budget não clamparia nada."""
    cap = context_cap(plan)
    if cap is None:
        return machine, False
    window = machine.get("max_model_len")
    if isinstance(window, int) and 0 < window <= cap:
        return machine, False
    return {**machine, "max_model_len": cap}, True


def plan_window(plan: str | None) -> dict:
    """Pseudo-máquina com a janela do plano, para o context_budget avaliar uma
    request que vai ao OpenRouter. Sem teto, `{}`: apply_context_budget vira
    no-op com max_model_len ausente."""
    cap = context_cap(plan)
    return {"max_model_len": cap} if cap is not None else {}
