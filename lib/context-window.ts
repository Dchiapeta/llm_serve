/**
 * Espelho em TS das funções de janela de contexto do gateway.
 *
 * Fonte de verdade: docker/gateway/context_budget.py
 *   RESERVED_OUTPUT_TOKENS      -> RESERVED_OUTPUT_TOKENS
 *   CONTEXT_TEMPLATE_OVERHEAD   -> TEMPLATE_OVERHEAD
 *   CONTEXT_EXACT_SAFETY_FACTOR -> EXACT_SAFETY_FACTOR
 *   usable_input_tokens()       -> usableInputTokens()
 *   auto_compact_window()       -> autoCompactWindow()
 *
 * A duplicação existe porque o painel calcula o snippet de configuração no
 * servidor, sem chave de cliente pra consultar o gateway. O acoplamento é o
 * teste test_janela_recomendada_ao_cliente (docker/gateway/test_context_budget.py),
 * que fixa os valores esperados e manda atualizar este arquivo se mudarem.
 */

/** Saída garantida ao cliente — piso de max_tokens do gateway (MIN_MAX_TOKENS). */
export const RESERVED_OUTPUT_TOKENS = 8000
/** Tokens do chat template (marcadores de role, BOS/EOS) fora do corpo JSON. */
export const TEMPLATE_OVERHEAD = 200
/**
 * Margem sobre a contagem exata do tokenizer. NÃO é o 1.2 da estimativa
 * heurística: quando o gateway conta de verdade (o que ele sempre faz perto do
 * teto) não há erro de estimativa a cobrir, só o resíduo entre o JSON contado e
 * o chat template renderizado.
 */
export const EXACT_SAFETY_FACTOR = 1.02

/** Fallback quando a janela é desconhecida (template sem --max-model-len, pod
 * anterior à migration 0031): assume o piso conservador da escada de planos
 * (64k), não a janela atual — errar pra baixo só compacta cedo, errar pra cima
 * faz o gateway rejeitar. */
const UNKNOWN_WINDOW_FALLBACK = 56000

/** Maior prompt que ainda deixa a saída garantida dentro da janela. */
export function usableInputTokens(maxModelLen: number): number {
  const room = maxModelLen - RESERVED_OUTPUT_TOKENS - TEMPLATE_OVERHEAD
  return Math.max(0, Math.floor(room / EXACT_SAFETY_FACTOR))
}

/**
 * Valor de CLAUDE_CODE_AUTO_COMPACT_WINDOW, arredondado pra baixo em 1k.
 *
 * Semântica não-óbvia: este valor NÃO é "compacte ao chegar aqui". É a
 * CAPACIDADE que o cliente passa a assumir (ele assume 200k para qualquer
 * ANTHROPIC_BASE_URL customizado e não tem como descobrir a real). O
 * auto-compact dispara numa fração INTERNA dele — entre ~80% e ~92%, varia por
 * versão do Claude Code e não é configurável.
 *
 * Por isso o valor é o MAIOR admissível, não 80% da janela: em 131072 → 120000,
 * e a compactação cai entre ~96k e ~110k (73%-84% da janela). Declarar 80%
 * (104000) faria compactar em 63%-73%, deixando ~15% de contexto na mesa.
 *
 * O teto é usableInputTokens porque acima dele o gateway rejeita — não sobraria
 * espaço para a resposta.
 */
export function autoCompactWindow(maxModelLen: number | null | undefined): number {
  if (!maxModelLen || maxModelLen <= 0) return UNKNOWN_WINDOW_FALLBACK
  return Math.floor(usableInputTokens(maxModelLen) / 1000) * 1000
}
