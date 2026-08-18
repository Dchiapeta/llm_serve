// Formatação de dinheiro da Chargefy.
//
// Vive fora de lib/chargefy.ts porque aquele módulo é server-only (carrega o
// segredo da API) e a tabela do CRM é um componente cliente. Aqui não há nada
// além de formatação pura.

/**
 * Valores da Chargefy são inteiros em centavos; a divisão só acontece aqui.
 *
 * O try/catch não é paranoia: `Intl.NumberFormat` lança RangeError para
 * qualquer código que não seja ISO-4217 de 3 letras, e o CRM usa o rótulo
 * "misto" quando uma conta tem assinaturas em moedas diferentes. Sem a guarda,
 * uma conta assim derrubaria a página inteira.
 */
export function formatMoney(cents: number, currency = "BRL"): string {
  try {
    return new Intl.NumberFormat("pt-BR", {
      style: "currency",
      currency,
    }).format(cents / 100)
  } catch {
    return `${(cents / 100).toLocaleString("pt-BR", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })} ${currency}`
  }
}

/** Assinaturas que contam como receita corrente. */
export const REVENUE_STATUSES = new Set(["active", "trialing", "past_due"])

const MONTHS_PER_INTERVAL: Record<string, number> = {
  day: 1 / 30.44,
  week: 1 / 4.345,
  month: 1,
  year: 12,
}

// Tipo estrutural mínimo: o que a conta precisa da assinatura, nada além.
// ChargefySubscription satisfaz isto, e manter a função aqui (fora do módulo
// que carrega o segredo) é o que permite testá-la sem subir o Next.
export type MonthlyInput = {
  currency: string
  items: {
    data: {
      amount_subtotal: number
      amount_total: number
      amount_discount?: number
      recurring: { interval: string; interval_count: number } | null
    }[]
  } | null
}

/**
 * Valor mensal equivalente da assinatura, em centavos.
 *
 * `gross` é o preço de tabela (amount_subtotal) e `net` o que de fato é cobrado
 * (amount_total, já com cupom). Os dois existem porque divergem de verdade: há
 * assinatura ativa cujo desconto zera a fatura, e exibir só um dos números faz
 * o CRM ou inflar a receita ou afirmar que um cliente ativo não paga.
 *
 * Soma TODOS os items — uma assinatura pode ter mais de uma linha.
 */
export function monthlyCents(sub: MonthlyInput): {
  gross: number
  net: number
  discount: number
  currency: string
} {
  let gross = 0
  let net = 0
  let discount = 0
  for (const item of sub.items?.data ?? []) {
    const rec = item.recurring
    const months = rec
      ? (MONTHS_PER_INTERVAL[rec.interval] ?? 1) *
        Math.max(1, rec.interval_count)
      : 1
    gross += item.amount_subtotal / months
    net += item.amount_total / months
    discount += (item.amount_discount ?? 0) / months
  }
  return {
    gross: Math.round(gross),
    net: Math.round(net),
    // `discount` é devolvido separado porque `gross !== net` NÃO significa
    // cupom: amount_total inclui amount_tax, então uma assinatura só com
    // imposto teria net MAIOR que gross e a UI anunciaria um desconto
    // inexistente, com o preço "cheio" riscado abaixo de um valor maior.
    discount: Math.round(discount),
    currency: (sub.currency || "brl").toUpperCase(),
  }
}
