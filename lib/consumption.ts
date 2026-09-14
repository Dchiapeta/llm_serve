// Unidade de consumo compartilhada entre LLM (tokens) e imagem (imagens
// geradas). Sem I/O — importável por Server e Client Components, mesmo
// padrão de lib/crm.ts.
//
// ---------------------------------------------------------------------------
// Por que existe
// ---------------------------------------------------------------------------
// Geração de imagem não produz token nenhum: gateway_requests.tokens_in/out
// fica NULL de propósito nas rotas de imagem, e usage_metrics grava
// requests>0 com tokens zerados. Toda superfície que somava
// `tokens_in + tokens_out` como "o" consumo mostrava 0 para uma stack de
// imagem, indistinguível na tela de "este cliente não usa o produto". O dado
// certo mora em image_generations (lido via a view image_usage_rollup,
// migration 0067) — este módulo é só a forma comum de somar e exibir as duas
// unidades juntas.
//
// ---------------------------------------------------------------------------
// Por que um produto ({tokens, images, requests}), não uma união marcada
// ---------------------------------------------------------------------------
// Uma união (`{kind: 'tokens'|'images', value}`) obrigaria cada ponto de soma
// a decidir o que fazer ao encontrar as duas unidades — e é exatamente em
// conta, CRM e dashboard global que elas aparecem juntas (uma conta pode ter
// stack de LLM e de imagem ao mesmo tempo). Como produto, somar é `add`
// trivial e componente a componente; a única decisão — "como isso se lê" —
// acontece uma vez, em `formatConsumption`, na hora de renderizar.

import { formatTokens } from "./crm"
import type { ProductCategory } from "./types"

export type ConsumptionUnit = "tokens" | "images"

export type Consumption = {
  tokens: number
  images: number
  requests: number
}

export const EMPTY_CONSUMPTION: Consumption = {
  tokens: 0,
  images: 0,
  requests: 0,
}

export function addConsumption(a: Consumption, b: Consumption): Consumption {
  return {
    tokens: a.tokens + b.tokens,
    images: a.images + b.images,
    requests: a.requests + b.requests,
  }
}

/**
 * Unidade de consumo nativa da categoria. Fail-open para "tokens" em
 * categoria nula/desconhecida — mesma disciplina de outros pontos do painel
 * que ramificam por `category` (ex.: sectionsForCategory).
 */
export function unitForCategory(
  category: ProductCategory | string | null
): ConsumptionUnit {
  return category === "image" ? "images" : "tokens"
}

/**
 * Unidades com valor > 0 num consumo agregado. Vazio = sem uso nenhum no
 * período; duas = conta/máquina/período com stack de LLM e de imagem juntas.
 */
export function unitsPresent(c: Consumption): ConsumptionUnit[] {
  const units: ConsumptionUnit[] = []
  if (c.tokens > 0) units.push("tokens")
  if (c.images > 0) units.push("images")
  return units
}

// Mesmas faixas de formatTokens (lib/crm.ts): números de imagem hoje são
// ordens de grandeza menores, mas a faixa alta existe para não quebrar se um
// cliente gerar milhões.
function formatImageCount(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`
  return String(value)
}

/**
 * Formata um consumo agregado para exibição numa coluna/rótulo genérico
 * ("Consumo"), onde o cabeçalho não pode mais implicar uma unidade só.
 *
 * Sem `unit` explícito, deriva de `unitsPresent`: uma unidade presente vira
 * "1,2M tokens" ou "87 imagens"; as duas juntas (conta mista) viram
 * "1,2M tokens · 87 imagens"; nenhuma vira "—" — nunca "0", que afirmaria uma
 * medição que não houve.
 *
 * `formatConsumption(c, "tokens")` delega em `formatTokens` (lib/crm.ts): a
 * bucketização/arredondamento do número nunca duplica lógica entre os dois
 * módulos, e o caminho de LLM continua vendo os mesmos números de sempre —
 * só ganha a palavra "tokens" ao lado, necessária porque o cabeçalho genérico
 * não a implica mais.
 */
export function formatConsumption(
  c: Consumption,
  unit?: ConsumptionUnit
): string {
  if (unit === "tokens") return `${formatTokens(c.tokens)} tokens`
  if (unit === "images") return `${formatImageCount(c.images)} imagens`

  const present = unitsPresent(c)
  if (present.length === 0) return "—"
  if (present.length === 1) return formatConsumption(c, present[0])
  return `${formatTokens(c.tokens)} tokens · ${formatImageCount(c.images)} imagens`
}

/**
 * Único escalar comparável entre as duas unidades — usar para ordenação onde
 * hoje se ordenava por tokens. Não é `tokens + images`: somar as duas
 * grandezas produziria um número sem significado (1 imagem custa muito mais
 * GPU que 1 token). `requests` é o que as duas unidades produzem com a mesma
 * semântica.
 */
export function consumptionSortKey(c: Consumption): number {
  return c.requests
}
