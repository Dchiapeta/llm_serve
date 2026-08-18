// Modelo e lógica pura do CRM. Sem I/O — a montagem das linhas vive em
// app/(dashboard)/crm/queries.ts (server).
//
// Este arquivo é importado tanto pelo servidor quanto pela tabela cliente, por
// isso não pode importar lib/chargefy.ts em runtime (o segredo da Chargefy não
// pode entrar no bundle do browser). Só tipos e funções puras.

import type { BillingStatus, TemplatePlan } from "./types"
import { TEMPLATE_PLANS } from "./types"

/** Como a assinatura da Chargefy foi ligada à conta — nem todo elo é igual. */
export type MatchSource =
  | "client_reference"
  | "account_id"
  | "checkout"
  | "customer_map"
  | "email"

/**
 * Conta interna (admin do painel), lead sem contratação, ou cliente de fato.
 *
 * Existe porque o /signup do painel cria uma linha em `accounts`
 * (ensureAccountForUser), então admins ficam misturados aos clientes na mesma
 * tabela. Sem essa separação o CRM contaria a própria equipe como cliente.
 */
export type CrmKind = "cliente" | "interno" | "sem_contratacao"

export type CrmStackRow = {
  id: string
  slug: string
  name: string
  plan: TemplatePlan
  machineId: string | null
  machineName: string | null
  billingStatusDb: BillingStatus
  pastDueSince: string | null
  usageClass: string
  purchaseDate: string
  provisioningRef: string | null
  /** null = nunca usada de fato (ver nota sobre o default now() em queries.ts) */
  lastActivityAt: string | null
  tokens: number
  requests: number
  activeKeys: number
  envs: number
  gpuCostUsd: number
  subscriptionId: string | null
  subscriptionStatus: string | null
}

export type CrmRow = {
  accountId: string
  name: string
  email: string | null
  kind: CrmKind

  stacks: CrmStackRow[]
  plans: TemplatePlan[]

  /** Rollup pior-caso das stacks. null = conta sem stack. */
  billingStatus: BillingStatus | null
  pastDueSince: string | null

  chargefyCustomerId: string | null
  matchSource: MatchSource | null
  subscriptionStatuses: string[]
  /** Banco e Chargefy discordam sobre o estado da assinatura. */
  subscriptionDivergence: boolean
  /** Assinatura paga cujo client_reference não casa com nenhuma stack. */
  orphanSubscriptions: number

  /** Centavos. gross = preço de tabela, net = cobrado (após cupom). */
  monthlyGrossCents: number
  monthlyNetCents: number
  currency: string
  /** Assinaturas em moedas diferentes — o total somado não tem significado. */
  mixedCurrency: boolean
  hasDiscount: boolean

  nextBillingAt: string | null
  cancelAtPeriodEnd: boolean
  customerSince: string

  lastUsedAt: string | null
  tokens: number
  requests: number

  activeKeys: number
  keyLimit: number | null
  envs: number
  envLimit: number | null

  gpuCostUsd: number
  /** Receita − custo, em BRL. null quando não há receita conhecida. */
  marginBrlCents: number | null
}

export type CrmKpis = {
  mrrCents: number
  mrrGrossCents: number
  payingCustomers: number
  activeCustomers: number
  atRiskCustomers: number
  atRiskCents: number
  avgTicketCents: number
  tokens: number
  requests: number
  gpuCostUsd: number
  marginBrlCents: number
  /** false quando alguma conta ficou sem receita conhecida (Chargefy fora). */
  marginKnown: boolean
  /** Contas classificadas como cliente que têm assinatura conhecida. */
  withSubscription: number
  totalCustomers: number
}

// Ordem do que EXIGE AÇÃO, não de "pior estado".
//
// 'canceled' fica de fora de propósito: uma stack cancelada meses atrás é
// história, não pendência. Rankeá-la como a pior faria um cliente com 3 stacks
// ativas e 1 antiga cancelada aparecer como "Cancelada" e jogaria o MRR inteiro
// dele no KPI "Em risco" — o oposto do que a coluna existe para mostrar.
const BILLING_SEVERITY: BillingStatus[] = [
  "suspended",
  "past_due",
  "trialing",
  "active",
]

/**
 * Estado de cobrança da conta a partir das stacks.
 *
 * 'canceled' só vence quando é o único estado presente — aí o cliente de fato
 * saiu.
 */
export function worstBillingStatus(
  statuses: BillingStatus[]
): BillingStatus | null {
  if (statuses.length === 0) return null
  for (const s of BILLING_SEVERITY) {
    if (statuses.includes(s)) return s
  }
  return statuses.every((s) => s === "canceled") ? "canceled" : null
}

/** Ordena planos pela escada comercial, não alfabeticamente. */
export function sortPlans(plans: TemplatePlan[]): TemplatePlan[] {
  return [...plans].sort(
    (a, b) => TEMPLATE_PLANS.indexOf(a) - TEMPLATE_PLANS.indexOf(b)
  )
}

/**
 * KPIs do conjunto recebido.
 *
 * Recebe as linhas JÁ FILTRADAS: quem decide o recorte é applyCrmFilters (o
 * escopo "clientes" é o default). Refiltrar por `kind` aqui faria os cards
 * contarem um conjunto e a tabela mostrar outro — e um filtro de "contas
 * internas" exibiria cards de clientes.
 */
export function summarizeCrm(rows: CrmRow[]): CrmKpis {
  const clientes = rows
  const paying = clientes.filter((r) => r.monthlyNetCents > 0)
  const atRisk = clientes.filter(
    (r) =>
      r.billingStatus === "past_due" ||
      r.billingStatus === "suspended" ||
      r.billingStatus === "canceled"
  )

  const sevenDaysAgo = Date.now() - 7 * 864e5
  const active = clientes.filter(
    (r) => r.lastUsedAt && new Date(r.lastUsedAt).getTime() >= sevenDaysAgo
  )

  const mrrCents = sum(clientes, (r) => r.monthlyNetCents)

  return {
    mrrCents,
    mrrGrossCents: sum(clientes, (r) => r.monthlyGrossCents),
    payingCustomers: paying.length,
    activeCustomers: active.length,
    atRiskCustomers: atRisk.length,
    atRiskCents: sum(atRisk, (r) => r.monthlyNetCents),
    avgTicketCents: paying.length ? Math.round(mrrCents / paying.length) : 0,
    tokens: sum(clientes, (r) => r.tokens),
    requests: sum(clientes, (r) => r.requests),
    gpuCostUsd: sum(clientes, (r) => r.gpuCostUsd),
    // Sem `?? 0` escondendo linha: com a Chargefy no ar toda conta tem margem
    // (negativa se consome e não paga), então a soma inclui o custo de quem
    // não gera receita — que é justamente o que o card ao lado cobra.
    marginBrlCents: sum(clientes, (r) => r.marginBrlCents ?? 0),
    marginKnown: clientes.every((r) => r.marginBrlCents !== null),
    withSubscription: clientes.filter((r) => r.subscriptionStatuses.length > 0)
      .length,
    totalCustomers: clientes.length,
  }
}

/** Linhas visíveis + os KPIs desse mesmo conjunto, para não divergirem. */
export function selectCrm(
  rows: CrmRow[],
  filters: CrmFilters
): { visible: CrmRow[]; kpis: CrmKpis } {
  const visible = applyCrmFilters(rows, filters)
  return { visible, kpis: summarizeCrm(visible) }
}

function sum<T>(items: T[], pick: (item: T) => number): number {
  return items.reduce((acc, item) => acc + pick(item), 0)
}

// ---------- Filtros ----------

export type CrmScope = "clientes" | "todos" | "internos"

export type CrmFilters = {
  q?: string
  plano?: string
  cobranca?: string
  escopo?: CrmScope
}

export function parseScope(value?: string | null): CrmScope {
  return value === "todos" || value === "internos" ? value : "clientes"
}

export function applyCrmFilters(rows: CrmRow[], f: CrmFilters): CrmRow[] {
  const escopo = f.escopo ?? "clientes"
  const q = f.q?.trim().toLowerCase()

  return rows.filter((row) => {
    if (escopo === "clientes" && row.kind !== "cliente") return false
    if (escopo === "internos" && row.kind !== "interno") return false

    if (f.plano && !row.plans.includes(f.plano as TemplatePlan)) return false
    if (f.cobranca && row.billingStatus !== f.cobranca) return false

    if (q) {
      const haystack = [
        row.name,
        row.email ?? "",
        row.accountId,
        ...row.stacks.map((s) => s.slug),
      ]
        .join(" ")
        .toLowerCase()
      if (!haystack.includes(q)) return false
    }
    return true
  })
}

// ---------- CSV ----------

const CSV_COLUMNS: { header: string; pick: (r: CrmRow) => string | number }[] = [
  { header: "Conta", pick: (r) => r.name },
  { header: "E-mail", pick: (r) => r.email ?? "" },
  { header: "ID", pick: (r) => r.accountId },
  { header: "Tipo", pick: (r) => r.kind },
  { header: "Planos", pick: (r) => r.plans.join(" / ") },
  { header: "Stacks", pick: (r) => r.stacks.length },
  { header: "Cobranca (banco)", pick: (r) => r.billingStatus ?? "" },
  { header: "Assinatura (Chargefy)", pick: (r) => r.subscriptionStatuses.join(" / ") },
  { header: "Divergencia", pick: (r) => (r.subscriptionDivergence ? "sim" : "") },
  { header: "Mensal tabela", pick: (r) => decimal(r.monthlyGrossCents / 100) },
  { header: "Mensal cobrado", pick: (r) => decimal(r.monthlyNetCents / 100) },
  { header: "Moeda", pick: (r) => r.currency },
  { header: "Cupom", pick: (r) => (r.hasDiscount ? "sim" : "") },
  { header: "Proxima cobranca", pick: (r) => date(r.nextBillingAt) },
  { header: "Cliente desde", pick: (r) => date(r.customerSince) },
  { header: "Ultimo uso", pick: (r) => date(r.lastUsedAt) },
  { header: "Tokens", pick: (r) => r.tokens },
  { header: "Requests", pick: (r) => r.requests },
  { header: "Chaves ativas", pick: (r) => r.activeKeys },
  { header: "Ambientes", pick: (r) => r.envs },
  { header: "Custo GPU USD", pick: (r) => decimal(r.gpuCostUsd) },
  {
    header: "Margem BRL",
    pick: (r) => (r.marginBrlCents === null ? "" : decimal(r.marginBrlCents / 100)),
  },
]

function decimal(value: number): string {
  // Decimal com vírgula: o consumidor é Excel pt-BR.
  return value.toFixed(2).replace(".", ",")
}

function date(iso: string | null): string {
  return iso ? formatDate(iso) : ""
}

/**
 * CSV para Excel pt-BR: separador ';', decimal ',', CRLF e BOM (adicionado no
 * route handler).
 *
 * O escape de '=' e companhia não é decoração: nome de conta é texto que o
 * próprio cliente escolheu, e uma célula começando com '=' é executada como
 * fórmula ao abrir a planilha.
 */
export function toCsv(rows: CrmRow[]): string {
  const lines = [CSV_COLUMNS.map((c) => escapeCsv(c.header)).join(";")]
  for (const row of rows) {
    lines.push(
      CSV_COLUMNS.map((c) => escapeCsv(String(c.pick(row)))).join(";")
    )
  }
  return lines.join("\r\n")
}

// Números negativos são dado, não fórmula: sem esta exceção uma margem de
// -123,45 sairia como o texto '-123,45 e o Excel deixaria de somar a coluna.
const NUMERIC_VALUE = /^-?\d+(?:[.,]\d+)?$/

function escapeCsv(value: string): string {
  const guarded =
    /^[=+\-@]/.test(value) && !NUMERIC_VALUE.test(value) ? `'${value}` : value
  return /[;"\r\n]/.test(guarded) ? `"${guarded.replace(/"/g, '""')}"` : guarded
}

// ---------- Formatação compartilhada ----------

// Fuso fixo do Brasil (sem DST desde 2019), igual ao TZ de lib/billing.ts: o
// server pode rodar em UTC e as datas precisam bater com o relógio de quem olha.
const CRM_TZ = "America/Sao_Paulo"

/**
 * Data em pt-BR.
 *
 * O ramo do `date` puro não é preciosismo: `stacks.purchase_date` é um DATE do
 * Postgres e chega como "2026-01-01". `new Date()` interpreta isso como
 * meia-noite UTC e, num fuso negativo como o do Brasil, a tela mostraria
 * 31/12/2025 — a data de compra do cliente errada por um dia.
 */
export function formatDate(iso: string | null): string {
  if (!iso) return "—"
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
  if (dateOnly) return `${dateOnly[3]}/${dateOnly[2]}/${dateOnly[1]}`
  // timeZone explícito: o server roda em UTC (Railway) e sem isto um timestamp
  // entre 00:00 e 03:00 UTC apareceria um dia à frente — o mesmo erro de um dia
  // que o ramo acima existe para evitar. Mesmo fuso fixo usado em lib/billing.ts.
  return new Date(iso).toLocaleDateString("pt-BR", { timeZone: CRM_TZ })
}

export function formatRelativeDay(iso: string | null): string {
  if (!iso) return "nunca"
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 864e5)
  if (days <= 0) return "hoje"
  if (days === 1) return "ontem"
  if (days < 30) return `há ${days}d`
  // O corte para anos usa o MESMO divisor da conta de anos. Trocar de faixa aos
  // 12 "meses de 30 dias" (360 dias) e depois dividir por 365 fazia 360–364
  // dias renderizarem "há 0a" — um cliente parado há quase um ano parecia
  // ativo, que é o oposto do que a coluna serve para mostrar.
  if (days < 365) return `há ${Math.floor(days / 30)}m`
  return `há ${Math.floor(days / 365)}a`
}

export function formatTokens(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`
  return String(value)
}
