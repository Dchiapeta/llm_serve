// Cliente de LEITURA da API da Chargefy (https://api.chargefy.io/v1).
//
// Só GET. Toda escrita (checkout, customers, webhooks) vive no repo TryStac —
// aqui o painel apenas lê para montar o CRM.
//
// Por que ler da API e não do banco: o espelho `chargefy_subscriptions` é
// populado por webhook do TryStac e hoje está VAZIO, enquanto a API tem
// assinaturas ativas em livemode. Como consequência, `stacks.billing_status`
// (escrito pelo trigger que projeta aquela tabela) também não é confiável. A
// API é a única fonte de verdade sobre cobrança; o valor do banco entra na UI
// só como comparação, para a divergência ficar visível em vez de escondida.
//
// ATENÇÃO: módulo server-only. CHARGEFY_SECRET_KEY não pode vazar para o
// browser — nenhum arquivo "use client" pode importar daqui, e os objetos crus
// (que trazem documento, endereço de cobrança e id de meio de pagamento) nunca
// devem ser passados inteiros para um componente de tabela.

import { unstable_cache } from "next/cache"

const API_BASE = process.env.CHARGEFY_API_BASE ?? "https://api.chargefy.io/v1"
const TIMEOUT_MS = 8_000
const PAGE_SIZE = 100
// Teto de páginas: 2.000 objetos. Acima disso a UI avisa que truncou em vez de
// exibir um MRR silenciosamente incompleto.
const MAX_PAGES = 20

// Devolve null em vez de lançar: a falta da chave degrada a página (mostra os
// dados do banco e um aviso), não a derruba.
function secret(): string | null {
  return process.env.CHARGEFY_SECRET_KEY ?? null
}

export type ChargefyList<T> = {
  object: "list"
  data: T[]
  has_more: boolean
}

export type ChargefyRecurring = {
  interval: "day" | "week" | "month" | "year"
  interval_count: number
}

export type ChargefySubscriptionItem = {
  id: string
  price: string | null
  product: string | null
  quantity: number
  unit_amount: number
  amount_subtotal: number
  amount_discount: number
  amount_tax: number
  amount_total: number
  currency: string
  recurring: ChargefyRecurring | null
}

export type ChargefySubscription = {
  id: string
  status: string
  currency: string
  customer: string
  livemode: boolean
  items: ChargefyList<ChargefySubscriptionItem>
  current_period_start: string | null
  current_period_end: string | null
  next_billing_at: string | null
  start_date: string | null
  trial_start: string | null
  trial_end: string | null
  cancel_at: string | null
  cancel_at_period_end: boolean
  canceled_at: string | null
  ended_at: string | null
  discount: string | null
  installments: number | null
  collection_method: string | null
  latest_invoice: string | null
  metadata: Record<string, string> | null
  created_at: string
}

export type ChargefyCustomer = {
  id: string
  email: string | null
  name: string | null
  billing_name: string | null
  document: string | null
  document_type: string | null
  phone: string | null
  livemode: boolean
  metadata: Record<string, string> | null
  created_at: string
}

export type ChargefyInvoice = {
  id: string
  number: string | null
  status: string
  currency: string
  customer: string | null
  customer_email: string | null
  subscription: string | null
  amount_total: number
  amount_subtotal: number
  amount_discount: number
  amount_due: number
  amount_paid: number
  amount_remaining: number
  billing_reason: string | null
  attempt_count: number | null
  due_date: string | null
  paid_at: string | null
  hosted_invoice_url: string | null
  invoice_pdf_url: string | null
  livemode: boolean
  created_at: string
}

// Mensagem de erro sem o header Authorization — o texto chega a ser renderizado
// no aviso da página.
function sanitize(message: string): string {
  const key = secret()
  const clean = key ? message.split(key).join("***") : message
  return clean.length > 300 ? `${clean.slice(0, 300)}…` : clean
}

async function cf<T>(
  path: string,
  params?: Record<string, string | number | undefined>
): Promise<T> {
  const key = secret()
  if (!key) throw new Error("CHARGEFY_SECRET_KEY não configurada")

  const url = new URL(`${API_BASE}${path}`)
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v))
  }

  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${key}` },
    // quem cacheia é o unstable_cache abaixo; o fetch em si nunca cacheia
    cache: "no-store",
    signal: AbortSignal.timeout(TIMEOUT_MS),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(sanitize(`Chargefy GET ${path} → ${res.status}: ${text}`))
  }
  return res.json() as Promise<T>
}

// Percorre a paginação por cursor (starting_after) até has_more virar false.
// `truncated` sobe para a UI quando o teto de páginas é atingido.
async function listAll<T extends { id: string }>(
  path: string,
  params?: Record<string, string | number | undefined>
): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = []
  let cursor: string | undefined

  for (let page = 0; page < MAX_PAGES; page++) {
    const res = await cf<ChargefyList<T>>(path, {
      ...params,
      limit: PAGE_SIZE,
      starting_after: cursor,
    })
    items.push(...res.data)
    if (!res.has_more || res.data.length === 0) {
      return { items, truncated: false }
    }
    cursor = res.data[res.data.length - 1].id
  }
  return { items, truncated: true }
}

export type ChargefySnapshot = {
  ok: boolean
  reason: "ok" | "missing_key" | "error"
  error: string | null
  fetchedAt: number
  truncated: boolean
  subscriptions: ChargefySubscription[]
  customers: ChargefyCustomer[]
}

// A função cacheada LANÇA em caso de erro, de propósito: se o erro virasse um
// valor de retorno, uma indisponibilidade de 1s ficaria cravada no cache por 60
// segundos. Quem trata é getChargefySnapshot, fora do cache.
const fetchSnapshot = unstable_cache(
  async (): Promise<Omit<ChargefySnapshot, "ok" | "reason" | "error">> => {
    const [subs, customers] = await Promise.all([
      listAll<ChargefySubscription>("/subscriptions"),
      listAll<ChargefyCustomer>("/customers"),
    ])
    // A chave é ch_live_; objeto de teste que porventura apareça não pode
    // inflar o MRR.
    return {
      fetchedAt: Date.now(),
      truncated: subs.truncated || customers.truncated,
      subscriptions: subs.items.filter((s) => s.livemode),
      customers: customers.items.filter((c) => c.livemode),
    }
  },
  ["chargefy-snapshot"],
  { revalidate: 60, tags: ["chargefy"] }
)

const EMPTY = {
  fetchedAt: 0,
  truncated: false,
  subscriptions: [] as ChargefySubscription[],
  customers: [] as ChargefyCustomer[],
}

/** Nunca rejeita: em caso de falha devolve snapshot vazio com o motivo. */
export async function getChargefySnapshot(): Promise<ChargefySnapshot> {
  if (!secret()) {
    return { ok: false, reason: "missing_key", error: null, ...EMPTY }
  }
  try {
    const snap = await fetchSnapshot()
    return { ok: true, reason: "ok", error: null, ...snap }
  } catch (e) {
    return {
      ok: false,
      reason: "error",
      error: sanitize(e instanceof Error ? e.message : String(e)),
      ...EMPTY,
    }
  }
}

/** Faturas de um cliente. Usada só na página de detalhe, sem cache global. */
export async function listInvoicesForCustomer(
  customerId: string
): Promise<{ ok: boolean; error: string | null; invoices: ChargefyInvoice[] }> {
  if (!secret()) return { ok: false, error: null, invoices: [] }
  try {
    const { items } = await listAll<ChargefyInvoice>("/invoices", {
      customer: customerId,
    })
    const invoices = items
      .filter((i) => i.livemode)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
    return { ok: true, error: null, invoices }
  } catch (e) {
    return {
      ok: false,
      error: sanitize(e instanceof Error ? e.message : String(e)),
      invoices: [],
    }
  }
}

// Dinheiro: tudo em CENTAVOS inteiros, divisão só na formatação. A conta mora
// em chargefy-format.ts, um módulo puro — assim ela é importável pela tabela
// (componente cliente) e testável sem subir o Next.
export { formatMoney, monthlyCents, REVENUE_STATUSES } from "./chargefy-format"
