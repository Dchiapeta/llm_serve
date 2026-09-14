// Unidade de consumo compartilhada entre LLM e imagem: TOKENS, com a contagem
// de imagens geradas como detalhe. Sem I/O — importável por Server e Client
// Components, mesmo padrão de lib/crm.ts.
//
// ---------------------------------------------------------------------------
// Por que tokens valem para imagem também
// ---------------------------------------------------------------------------
// Difusão não gera texto, mas o transformer do FLUX.2 processa uma sequência
// de tokens como qualquer outro: o prompt mais um token por patch latente de
// 16×16 px de cada imagem (gerada ou de referência). Desde a imagem 0.1.6 o
// pod devolve essa conta num bloco `usage` no formato do vLLM, o agent a soma
// em usage_metrics e o gateway a grava em gateway_requests.tokens_in/out —
// pelos MESMOS caminhos das rotas de texto (docker/image/policy.usage_block).
// Uma stack de imagem, portanto, tem consumo em tokens como qualquer outra;
// `images` (image_usage_rollup, migration 0067) continua existindo porque
// "quantas imagens" é uma pergunta que tokens não respondem.
//
// Pod anterior à 0.1.6 não manda `usage`: tokens ficam nulos e a stack volta
// a aparecer só com o número de imagens — é o sinal de que a máquina ainda
// não foi recriada com a imagem nova.
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

// Mesmas faixas de formatTokens (lib/crm.ts): números de imagem hoje são
// ordens de grandeza menores, mas a faixa alta existe para não quebrar se um
// cliente gerar milhões.
export function formatImageCount(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`
  return String(value)
}

/**
 * Formata um consumo agregado para exibição numa coluna/rótulo genérico
 * ("Consumo").
 *
 * Tokens são a manchete, sempre: é a unidade que as duas categorias produzem
 * e a que a cota diária e o rateio de custo usam. Imagens entram como sufixo
 * quando existem — "1,2M tokens · 87 imagens" — porque contam algo que os
 * tokens não contam. Sem nada dos dois vira "—", nunca "0", que afirmaria
 * uma medição que não houve. O caso "0 tokens · 87 imagens" é legítimo e
 * proposital: é a cara de uma máquina de imagem que ainda roda um pod
 * anterior à 0.1.6.
 *
 * Delega em `formatTokens` (lib/crm.ts): a bucketização/arredondamento do
 * número nunca duplica lógica entre os dois módulos.
 */
export function formatConsumption(c: Consumption): string {
  if (c.tokens <= 0 && c.images <= 0) return "—"
  const parts = [`${formatTokens(c.tokens)} tokens`]
  if (c.images > 0) parts.push(`${formatImageCount(c.images)} imagens`)
  return parts.join(" · ")
}

/**
 * Escalar para ordenação onde se ordena por consumo. Tokens, porque é a
 * unidade em que as duas categorias custam GPU — `images` só desempata uma
 * máquina de imagem em pod antigo, que não conta tokens.
 */
export function consumptionSortKey(c: Consumption): number {
  return c.tokens > 0 ? c.tokens : c.images
}
