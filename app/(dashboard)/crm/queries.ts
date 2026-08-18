// Montagem das linhas do CRM: banco + Chargefy.
//
// Server-only (importa lib/chargefy.ts). A lógica pura de filtro/CSV vive em
// lib/crm.ts, que é compartilhado com a tabela cliente.

import { cache } from "react"

import { requireAdminSession } from "@/lib/auth-admin-server"
import {
  monthlyCents,
  REVENUE_STATUSES,
  getChargefySnapshot,
  type ChargefySnapshot,
  type ChargefySubscription,
} from "@/lib/chargefy"
import { summarizeCost, costWindow, type RuntimeInterval } from "@/lib/billing"
import {
  worstBillingStatus,
  sortPlans,
  type CrmRow,
  type CrmStackRow,
  type MatchSource,
} from "@/lib/crm"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import {
  CLIENT_WINDOW_DAYS,
  MAX_CLIENTS_BY_PLAN,
  MAX_KEYS_BY_PLAN,
  type Account,
  type BillingStatus,
  type Machine,
  type TemplatePlan,
} from "@/lib/types"
import { isAllowedAdminEmail } from "@/lib/auth-admin"

export type CrmPeriod = "24h" | "7d" | "30d" | "total"

export const CRM_PERIOD_MS: Record<CrmPeriod, number | null> = {
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
  "30d": 30 * 24 * 3600_000,
  total: null,
}

export const CRM_PERIOD_LABELS: Record<CrmPeriod, string> = {
  "24h": "últimas 24 horas",
  "7d": "últimos 7 dias",
  "30d": "últimos 30 dias",
  total: "todo o histórico",
}

const CRM_PERIODS: readonly CrmPeriod[] = ["24h", "7d", "30d", "total"]

export function parsePeriod(value?: string | null): CrmPeriod {
  // Lista explícita, não `value in CRM_PERIOD_MS`: `in` alcança o protótipo, e
  // `?period=constructor` passaria pela validação. periodMs viraria uma função,
  // a janela viraria NaN e — como NaN falha todas as comparações, inclusive os
  // guardas `<= 0` de overlapHours — todo o custo de GPU renderizaria como NaN.
  //
  // A lista também precisa cobrir TODAS as opções que a UI oferece: um valor
  // válido caindo no default renderiza outro período e o seletor volta sozinho
  // para "30 dias".
  return value != null && (CRM_PERIODS as readonly string[]).includes(value)
    ? (value as CrmPeriod)
    : "30d"
}

// Câmbio para comparar receita (BRL, Chargefy) com custo de GPU (USD, RunPod).
// É estimativa e a UI diz isso — misturar as duas moedas sem rótulo produz
// exatamente o tipo de número que vira decisão errada.
// Number("abc") é NaN e contaminaria toda a margem silenciosamente.
const RATE_FROM_ENV = Number(process.env.USD_BRL_RATE)
export const USD_BRL_RATE =
  Number.isFinite(RATE_FROM_ENV) && RATE_FROM_ENV > 0 ? RATE_FROM_ENV : 5.4

// O PostgREST devolve no máximo 1000 linhas por resposta. usage_metrics cresce
// ~1 linha por chave a cada 2 min: agregar com um select simples truncaria em
// silêncio e o CRM passaria a subnotificar tokens sem nenhum erro visível.
const PAGE = 1000
const MAX_PAGES = 50

type PagedQuery = {
  range: (
    from: number,
    to: number
  ) => PromiseLike<{ data: unknown[] | null; error: { message: string } | null }>
}

/**
 * Paginação completa de uma query.
 *
 * A query passada precisa estar ordenada por uma coluna ÚNICA (id). Ordenar por
 * algo repetido — `window_start`, que se repete a cada janela de 2 min — deixa a
 * ordem indefinida entre linhas empatadas, e aí o Postgres pode devolver a mesma
 * linha em duas páginas e pular outra: os totais de token ficariam errados sem
 * nenhum sinal.
 *
 * Erro NÃO é tratado como fim dos dados: sem checar `error`, uma falha devolve
 * data=null, o laço interpreta como "acabou" e a página exibe 0 tokens com o
 * aviso de truncamento desligado — o pior resultado possível, que é mentir
 * calado.
 */
async function fetchAll<T>(
  build: () => PagedQuery
): Promise<{ rows: T[]; truncated: boolean; failed: boolean }> {
  const rows: T[] = []
  for (let page = 0; page < MAX_PAGES; page++) {
    const from = page * PAGE
    const { data, error } = await build().range(from, from + PAGE - 1)
    if (error) return { rows, truncated: false, failed: true }
    const batch = (data ?? []) as T[]
    rows.push(...batch)
    if (batch.length < PAGE) return { rows, truncated: false, failed: false }
  }
  return { rows, truncated: true, failed: false }
}

type StackRecord = {
  id: string
  account_id: string
  machine_id: string | null
  plan: TemplatePlan
  slug: string
  name: string | null
  purchase_date: string
  usage_class: string
  billing_status: BillingStatus
  past_due_since: string | null
  provisioning_ref: string | null
  last_activity_at: string
  created_at: string
}

type KeyRecord = {
  id: string
  account_id: string
  stack_id: string | null
  status: string
  purpose: string
}

type UsageRecord = {
  stack_id: string | null
  api_key_id: string | null
  machine_id: string | null
  tokens_in: number
  tokens_out: number
  requests: number
}

export type CrmData = {
  /** Todas as contas, sem filtro — quem recorta é selectCrm (lib/crm.ts). */
  rows: CrmRow[]
  chargefy: Pick<
    ChargefySnapshot,
    "ok" | "reason" | "error" | "fetchedAt" | "truncated"
  >
  /** Assinaturas em livemode que não casaram com nenhuma conta. */
  unmatchedSubscriptions: { id: string; status: string; monthlyCents: number }[]
  /** Tokens de linhas de uso que não puderam ser atribuídas a uma conta. */
  unattributedTokens: number
  /** Uso ou custo vieram incompletos (teto de páginas ou erro na query). */
  usageTruncated: boolean
  costTruncated: boolean
  /** Alguma tabela base (contas, stacks, chaves, máquinas, Chargefy) falhou. */
  coreIncomplete: boolean
  /**
   * Gasto total de GPU no período — o mesmo número da página Financeiro. É
   * maior que a soma do rateio por cliente, porque máquina sem stack (teste,
   * pod órfão) custa dinheiro e não tem a quem ser atribuída. Exibido junto do
   * rateado para a diferença não parecer erro de conta.
   */
  gpuCostTotalUsd: number
}

/**
 * `cache()` do React: dedupe dentro da mesma request, para a página e a toolbar
 * não dispararem as queries duas vezes. Mesmo padrão de
 * app/(dashboard)/machines/queries.ts.
 */
export const getCrmData = cache(async function getCrmData(
  period: CrmPeriod
): Promise<CrmData> {
  // Camada de autorização mais próxima do dado — a que a doc do Next recomenda.
  await requireAdminSession()

  const db = createSupabaseAdmin()
  const periodMs = CRM_PERIOD_MS[period]
  const { from: windowFrom, to } = costWindow(periodMs)
  const since = windowFrom ? new Date(windowFrom).toISOString() : null
  const clientSince = new Date(
    Date.now() - CLIENT_WINDOW_DAYS * 864e5
  ).toISOString()

  const [
    accountsRes,
    stacksRes,
    keysRes,
    clientsRes,
    machinesRes,
    intervalsRes,
    cfCustomersRes,
    cfAttemptsRes,
    usageRes,
    chargefy,
  ] = await Promise.all([
    // Todas paginadas: o teto de 1000 linhas do PostgREST vale para qualquer
    // select. api_keys já está em 126 e machines em 108 — deixar sem paginar
    // significa que, ao crescer, a conta some sem nenhum sinal na tela.
    fetchAll<Account>(
      () => db.from("accounts").select("*").order("id") as unknown as PagedQuery
    ),
    fetchAll<StackRecord>(
      () =>
        db
          .from("stacks")
          .select(
            "id, account_id, machine_id, plan, slug, name, purchase_date, usage_class, billing_status, past_due_since, provisioning_ref, last_activity_at, created_at"
          )
          .order("id") as unknown as PagedQuery
    ),
    fetchAll<KeyRecord>(
      () =>
        db
          .from("api_keys")
          .select("id, account_id, stack_id, status, purpose")
          .order("id") as unknown as PagedQuery
    ),
    fetchAll<{ stack_id: string; status: string }>(
      () =>
        db
          .from("stack_clients")
          .select("stack_id, status, last_seen_at")
          .gte("last_seen_at", clientSince)
          .order("id") as unknown as PagedQuery
    ),
    fetchAll<Machine>(
      () => db.from("machines").select("*").order("id") as unknown as PagedQuery
    ),
    // Mesmo recorte de janela da página Financeiro (financeiro-body.tsx): sem
    // ele a query traz o histórico inteiro e estoura o teto de 1000 linhas do
    // PostgREST — só os intervalos MAIS ANTIGOS chegariam, o custo de GPU cairia
    // para ~0 e a margem passaria a ser a receita inteira.
    fetchAll<RuntimeInterval>(() => {
      const q = db.from("machine_runtime_intervals").select("*").order("id")
      return (
        since
          ? q
              .lte("started_at", new Date(to).toISOString())
              .or(`ended_at.is.null,ended_at.gte.${since}`)
          : q
      ) as unknown as PagedQuery
    }),
    // Também paginadas: são elos da cascata de match. Perder linhas aqui faria
    // assinaturas pagas caírem em "sem conta correspondente" e sumirem do MRR.
    fetchAll<{ account_id: string; chargefy_customer_id: string }>(
      () =>
        db
          .from("chargefy_customers")
          .select("account_id, chargefy_customer_id")
          .order("id") as unknown as PagedQuery
    ),
    fetchAll<{
      account_id: string | null
      stack_id: string | null
      client_reference: string | null
      chargefy_subscription_id: string | null
    }>(
      () =>
        db
          .from("chargefy_checkout_attempts")
          .select(
            "account_id, stack_id, client_reference, chargefy_subscription_id"
          )
          .order("id") as unknown as PagedQuery
    ),
    fetchAll<UsageRecord>(() => {
      // Ordenado por `id` (único), não por `window_start`: ver o comentário de
      // fetchAll — muitas linhas compartilham a mesma janela de 2 minutos.
      const q = db
        .from("usage_metrics")
        .select("stack_id, api_key_id, machine_id, tokens_in, tokens_out, requests")
        .order("id")
      return (since ? q.gte("window_start", since) : q) as unknown as PagedQuery
    }),
    getChargefySnapshot(),
  ])

  const accounts = accountsRes.rows
  const stacks = stacksRes.rows
  const keys = keysRes.rows
  const clients = clientsRes.rows
  const machines = machinesRes.rows
  const intervals = intervalsRes.rows

  // Qualquer leitura incompleta contamina a conta inteira — melhor gritar do
  // que exibir um CRM que perdeu contas ou stacks em silêncio.
  const coreIncomplete = [
    accountsRes,
    stacksRes,
    keysRes,
    clientsRes,
    machinesRes,
    cfCustomersRes,
    cfAttemptsRes,
  ].some((r) => r.truncated || r.failed)
  const cfCustomers = cfCustomersRes.rows
  const cfAttempts = cfAttemptsRes.rows
  const usage = usageRes.rows

  const keyById = new Map(keys.map((k) => [k.id, k]))

  // ---- Uso por stack (e por máquina, para o rateio de custo) ----
  const usageByStack = new Map<string, { tokens: number; requests: number }>()
  const usageByMachineStack = new Map<string, Map<string, number>>()
  let unattributedTokens = 0

  for (const u of usage) {
    const tokens = (u.tokens_in ?? 0) + (u.tokens_out ?? 0)
    // 89 de 711 linhas não têm stack_id; a chave resolve a maioria delas.
    const stackId =
      u.stack_id ?? (u.api_key_id ? keyById.get(u.api_key_id)?.stack_id : null)
    if (!stackId) {
      unattributedTokens += tokens
      continue
    }
    const agg = usageByStack.get(stackId) ?? { tokens: 0, requests: 0 }
    agg.tokens += tokens
    agg.requests += u.requests ?? 0
    usageByStack.set(stackId, agg)

    if (u.machine_id) {
      const perStack = usageByMachineStack.get(u.machine_id) ?? new Map()
      perStack.set(stackId, (perStack.get(stackId) ?? 0) + tokens)
      usageByMachineStack.set(u.machine_id, perStack)
    }
  }

  // ---- Custo de GPU rateado por stack ----
  // Reusa summarizeCost (lib/billing.ts), que já é a conta oficial da página
  // Financeiro — o CRM não pode divergir dela.
  //
  // No período "total" a borda esquerda é o intervalo MAIS ANTIGO, calculado
  // com Math.min — a lista vem ordenada por `id` (exigência da paginação), não
  // por `started_at`, então `intervals[0]` não é o mais antigo. Assumir que era
  // encurtava a janela e fazia "total" custar MENOS que "30 dias".
  const from =
    windowFrom ??
    (intervals.length > 0
      ? Math.min(...intervals.map((iv) => new Date(iv.started_at).getTime()))
      : to - 24 * 3600_000)
  const cost = summarizeCost(intervals, machines, from, to)

  const gpuCostByStack = new Map<string, number>()
  const stacksByMachine = new Map<string, StackRecord[]>()
  for (const s of stacks) {
    if (!s.machine_id) continue
    const list = stacksByMachine.get(s.machine_id) ?? []
    list.push(s)
    stacksByMachine.set(s.machine_id, list)
  }

  for (const row of cost.byMachine) {
    if (row.spent <= 0) continue
    const tokensPerStack = usageByMachineStack.get(row.machineId)
    const total = tokensPerStack
      ? [...tokensPerStack.values()].reduce((a, b) => a + b, 0)
      : 0

    if (tokensPerStack && total > 0) {
      // Rateio por consumo: quem gastou mais GPU carrega mais custo.
      for (const [stackId, tokens] of tokensPerStack) {
        gpuCostByStack.set(
          stackId,
          (gpuCostByStack.get(stackId) ?? 0) + (row.spent * tokens) / total
        )
      }
    } else {
      // Sem token no período, o custo existe mesmo assim (a máquina ficou
      // ligada). Divide igualmente entre as stacks hospedadas nela.
      const hosted = stacksByMachine.get(row.machineId) ?? []
      if (hosted.length === 0) continue
      for (const s of hosted) {
        gpuCostByStack.set(
          s.id,
          (gpuCostByStack.get(s.id) ?? 0) + row.spent / hosted.length
        )
      }
    }
  }

  // ---- Chaves e ambientes por stack ----
  const activeKeysByStack = new Map<string, number>()
  for (const k of keys) {
    // purpose 'playground' é chave interna da stack, nunca do cliente (0044).
    if (k.status !== "active" || k.purpose !== "customer" || !k.stack_id) continue
    activeKeysByStack.set(k.stack_id, (activeKeysByStack.get(k.stack_id) ?? 0) + 1)
  }
  const envsByStack = new Map<string, number>()
  for (const c of clients) {
    if (c.status !== "active") continue
    envsByStack.set(c.stack_id, (envsByStack.get(c.stack_id) ?? 0) + 1)
  }

  // ---- Ligação Chargefy -> conta, em cascata de confiança ----
  const accountByCustomerId = new Map(
    cfCustomers.map((c) => [c.chargefy_customer_id, c.account_id])
  )
  const stackByProvisioningRef = new Map(
    stacks.filter((s) => s.provisioning_ref).map((s) => [s.provisioning_ref!, s])
  )
  const attemptByRef = new Map(
    cfAttempts.filter((a) => a.client_reference).map((a) => [a.client_reference!, a])
  )
  const accountByEmail = new Map(
    accounts.filter((a) => a.email).map((a) => [a.email!.toLowerCase(), a.id])
  )
  const customerById = new Map(chargefy.customers.map((c) => [c.id, c]))

  type Matched = {
    sub: ChargefySubscription
    accountId: string | null
    stackId: string | null
    source: MatchSource | null
  }

  const matched: Matched[] = chargefy.subscriptions.map((sub) => {
    const meta = sub.metadata ?? {}
    const ref = meta.client_reference

    // 1. client_reference -> stack: o elo mais preciso (chave de idempotência
    //    do provisionamento).
    const stackByRef = ref ? stackByProvisioningRef.get(ref) : undefined
    if (stackByRef) {
      return {
        sub,
        accountId: stackByRef.account_id,
        stackId: stackByRef.id,
        source: "client_reference",
      }
    }
    // 2. metadata.account_id
    if (meta.account_id && accounts.some((a) => a.id === meta.account_id)) {
      return { sub, accountId: meta.account_id, stackId: null, source: "account_id" }
    }
    // 3. tentativa de checkout registrada no banco
    const attempt = ref ? attemptByRef.get(ref) : undefined
    if (attempt?.account_id) {
      return {
        sub,
        accountId: attempt.account_id,
        stackId: attempt.stack_id,
        source: "checkout",
      }
    }
    // 4. mapa de customers
    const mapped = accountByCustomerId.get(sub.customer)
    if (mapped) {
      return { sub, accountId: mapped, stackId: null, source: "customer_map" }
    }
    // 5. fallback frouxo por e-mail — marcado para a UI poder avisar
    const email = customerById.get(sub.customer)?.email?.toLowerCase()
    const byEmail = email ? accountByEmail.get(email) : undefined
    if (byEmail) {
      return { sub, accountId: byEmail, stackId: null, source: "email" }
    }
    return { sub, accountId: null, stackId: null, source: null }
  })

  const subsByAccount = new Map<string, Matched[]>()
  for (const m of matched) {
    if (!m.accountId) continue
    const list = subsByAccount.get(m.accountId) ?? []
    list.push(m)
    subsByAccount.set(m.accountId, list)
  }

  const stacksByAccount = new Map<string, StackRecord[]>()
  for (const s of stacks) {
    const list = stacksByAccount.get(s.account_id) ?? []
    list.push(s)
    stacksByAccount.set(s.account_id, list)
  }

  const machineById = new Map(machines.map((m) => [m.id, m]))

  // ---- Linhas ----
  const rows: CrmRow[] = accounts.map((account) => {
    const accStacks = stacksByAccount.get(account.id) ?? []
    const accSubs = subsByAccount.get(account.id) ?? []

    const stackRows: CrmStackRow[] = accStacks.map((s) => {
      const u = usageByStack.get(s.id) ?? { tokens: 0, requests: 0 }
      const sub = accSubs.find((m) => m.stackId === s.id)?.sub
      return {
        id: s.id,
        slug: s.slug,
        name: s.name ?? s.slug,
        plan: s.plan,
        machineId: s.machine_id,
        machineName: s.machine_id
          ? machineById.get(s.machine_id)?.name ?? null
          : null,
        billingStatusDb: s.billing_status,
        pastDueSince: s.past_due_since,
        usageClass: s.usage_class,
        purchaseDate: s.purchase_date,
        provisioningRef: s.provisioning_ref,
        lastActivityAt: effectiveLastActivity(s, u.requests),
        tokens: u.tokens,
        requests: u.requests,
        activeKeys: activeKeysByStack.get(s.id) ?? 0,
        envs: envsByStack.get(s.id) ?? 0,
        gpuCostUsd: gpuCostByStack.get(s.id) ?? 0,
        subscriptionId: sub?.id ?? null,
        subscriptionStatus: sub?.status ?? null,
      }
    })

    const revenueSubs = accSubs.filter((m) => REVENUE_STATUSES.has(m.sub.status))
    let gross = 0
    let net = 0
    let discount = 0
    const currencies = new Set<string>()
    for (const m of revenueSubs) {
      const money = monthlyCents(m.sub)
      gross += money.gross
      net += money.net
      discount += money.discount
      currencies.add(money.currency)
    }
    // Somar centavos de moedas diferentes produz um número sem significado
    // nenhum. Hoje é tudo BRL; se um dia não for, a linha diz "misto" em vez de
    // exibir um total falso com o símbolo da última assinatura processada.
    const currency = currencies.size > 1 ? "misto" : ([...currencies][0] ?? "BRL")
    const mixedCurrency = currencies.size > 1

    const plans = sortPlans([...new Set(stackRows.map((s) => s.plan))])
    const billingStatus = worstBillingStatus(
      stackRows.map((s) => s.billingStatusDb)
    )
    // A data tem que vir de uma stack que está NO estado exibido no badge, e a
    // mais antiga entre elas (a que corta primeiro). Pegar a primeira com data
    // preenchida faria o badge falar de uma stack e a contagem de outra.
    const pastDue =
      stackRows
        .filter((s) => s.billingStatusDb === billingStatus && s.pastDueSince)
        .map((s) => s.pastDueSince!)
        .sort()[0] ?? null

    const lastUsedAt = stackRows
      .map((s) => s.lastActivityAt)
      .filter((d): d is string => Boolean(d))
      .sort()
      .pop() ?? null

    const gpuCostUsd = stackRows.reduce((acc, s) => acc + s.gpuCostUsd, 0)

    // Mensaliza o custo do período para comparar com uma receita mensal.
    const periodDays = (to - from) / 864e5
    const gpuMonthlyUsd = periodDays > 0 ? (gpuCostUsd * 30) / periodDays : 0
    // null SÓ quando a Chargefy não respondeu — aí a receita é desconhecida.
    // Com ela disponível, receita 0 é uma resposta, não ausência de resposta:
    // um cliente sem assinatura que consome GPU tem margem NEGATIVA, e é
    // exatamente esse caso que o CRM existe para expor. Tratar como "—"
    // escondia o prejuízo e ainda inflava o KPI de margem.
    const marginBrlCents = chargefy.ok
      ? Math.round(net - gpuMonthlyUsd * USD_BRL_RATE * 100)
      : null

    const statuses = accSubs.map((m) => m.sub.status)
    const nextBilling = revenueSubs
      .map((m) => m.sub.next_billing_at ?? m.sub.current_period_end)
      .filter((d): d is string => Boolean(d))
      .sort()[0] ?? null

    const isInternal = isAllowedAdminEmail(account.email)
    const kind = isInternal
      ? "interno"
      : accStacks.length === 0 && accSubs.length === 0
        ? "sem_contratacao"
        : "cliente"

    // MAX_KEYS_BY_PLAN e MAX_CLIENTS_BY_PLAN são tetos POR STACK, e a linha é
    // por conta: comparar a soma da conta com o teto de um único plano faria
    // duas stacks Go em dia aparecerem como "6/3", inventando uma violação.
    // Somar os tetos de cada stack é o equivalente correto no nível da conta;
    // uma stack Enterprise (sem teto) torna o total ilimitado.
    const sumLimits = (
      table: Record<TemplatePlan, number | null>
    ): number | null => {
      if (stackRows.length === 0) return null
      let total = 0
      for (const s of stackRows) {
        const limit = table[s.plan]
        if (limit === null) return null
        total += limit
      }
      return total
    }
    const keyLimit = sumLimits(MAX_KEYS_BY_PLAN)
    const envLimit = sumLimits(MAX_CLIENTS_BY_PLAN)

    return {
      accountId: account.id,
      name: account.name,
      email: account.email,
      kind,
      stacks: stackRows,
      plans,
      billingStatus,
      pastDueSince: pastDue,
      chargefyCustomerId: accSubs[0]?.sub.customer ?? null,
      matchSource: accSubs[0]?.source ?? null,
      subscriptionStatuses: [...new Set(statuses)],
      // O banco diz uma coisa e a Chargefy outra. Acontece hoje porque o
      // espelho por webhook está parado — a UI mostra em vez de esconder.
      subscriptionDivergence:
        statuses.length > 0 &&
        billingStatus !== null &&
        !statuses.includes(billingStatus),
      // Só conta como órfã a assinatura que CARREGA um client_reference e
      // mesmo assim não achou stack — aí houve provisionamento perdido de
      // verdade. Contar todo sub sem stackId marcaria como "pagou e não
      // recebeu" qualquer conta ligada por customer_map ou e-mail, tiers que
      // nunca resolvem stack, pintando de vermelho cliente saudável.
      orphanSubscriptions: accSubs.filter(
        (m) => !m.stackId && (m.sub.metadata?.client_reference ?? null) !== null
      ).length,
      monthlyGrossCents: gross,
      monthlyNetCents: net,
      currency,
      mixedCurrency,
      // Cupom de verdade vem de amount_discount, não da diferença gross/net.
      hasDiscount: discount > 0,
      nextBillingAt: nextBilling,
      cancelAtPeriodEnd: revenueSubs.some((m) => m.sub.cancel_at_period_end),
      // O MENOR start_date, não o primeiro que a API devolveu: um cliente com
      // várias assinaturas apareceria como "cliente desde" a mais recente,
      // encurtando a relação dele com a gente.
      customerSince:
        revenueSubs
          .map((m) => m.sub.start_date)
          .filter((d): d is string => Boolean(d))
          .sort()[0] ??
        accStacks.map((s) => s.purchase_date).sort()[0] ??
        account.created_at,
      lastUsedAt,
      tokens: stackRows.reduce((acc, s) => acc + s.tokens, 0),
      requests: stackRows.reduce((acc, s) => acc + s.requests, 0),
      activeKeys: stackRows.reduce((acc, s) => acc + s.activeKeys, 0),
      keyLimit,
      envs: stackRows.reduce((acc, s) => acc + s.envs, 0),
      envLimit,
      gpuCostUsd,
      marginBrlCents,
    }
  })

  rows.sort((a, b) => b.monthlyNetCents - a.monthlyNetCents || b.tokens - a.tokens)

  return {
    rows,
    chargefy: {
      ok: chargefy.ok,
      reason: chargefy.reason,
      error: chargefy.error,
      fetchedAt: chargefy.fetchedAt,
      truncated: chargefy.truncated,
    },
    // Só as que ainda geram receita: uma assinatura cancelada sem dono é
    // história, e somá-la ao total inflaria o "R$ X/mês … o dinheiro é real".
    unmatchedSubscriptions: matched
      .filter((m) => !m.accountId && REVENUE_STATUSES.has(m.sub.status))
      .map((m) => ({
        id: m.sub.id,
        status: m.sub.status,
        monthlyCents: monthlyCents(m.sub).net,
      })),
    unattributedTokens,
    usageTruncated: usageRes.truncated || usageRes.failed,
    costTruncated: intervalsRes.truncated || intervalsRes.failed,
    // Sinal próprio: dobrar este caso em `usageTruncated` mandaria investigar
    // usage_metrics quando quem falhou foi accounts, stacks ou api_keys.
    coreIncomplete,
    gpuCostTotalUsd: cost.spent,
  }
})

/**
 * `stacks.last_activity_at` é `not null default now()`, então uma stack que
 * nunca recebeu request exibe a data de criação e parece recém-usada. Quando
 * não há request algum e o carimbo é praticamente o da criação, é "nunca".
 */
function effectiveLastActivity(
  stack: StackRecord,
  requests: number
): string | null {
  if (requests > 0) return stack.last_activity_at
  const delta =
    new Date(stack.last_activity_at).getTime() -
    new Date(stack.created_at).getTime()
  return Math.abs(delta) < 60_000 ? null : stack.last_activity_at
}
