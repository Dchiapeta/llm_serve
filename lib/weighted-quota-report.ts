// Análise da request ponderada (migration 0079) antes de ligar o corte.
//
// O gateway já calcula o uso ponderado — ceil(tokens_in / 32768), mínimo 1 —
// mas com REQUEST_QUOTA_ENFORCE desligado só registra no log. Esta é a leitura
// que decide a transição: quem passaria do limite com o peso novo (e precisa
// de stacks.request_quota_override antes do corte), se os tokens estão sendo
// gravados para dar peso, e como os tokens por requisição se distribuem.

import type { createSupabaseAdmin } from "@/lib/supabase/server"
import { fetchStackRequestQuotas, type RequestQuota } from "@/lib/request-quota"

type Db = ReturnType<typeof createSupabaseAdmin>

export const REPORT_DAYS = 30
export const QUOTA_PLANS = ["Go", "Pro"] as const
export type QuotaPlan = (typeof QUOTA_PLANS)[number]

// Abaixo disso a projeção de fim de ciclo é ruído: um dia de uso alto no
// começo do ciclo projetaria 30× o limite.
const MIN_PROJECTION_DAYS = 3
const DAY_MS = 24 * 60 * 60 * 1000

export const BUCKETS = [
  { key: "none", label: "Sem tokens_in" },
  { key: "le_8k", label: "≤ 8K" },
  { key: "le_32k", label: "8K – 32K" },
  { key: "le_64k", label: "32K – 64K (peso 2)" },
  { key: "le_128k", label: "64K – 128K (peso 3–4)" },
  { key: "le_256k", label: "128K – 256K (peso 5–8)" },
  { key: "gt_256k", label: "> 256K (peso 9+)" },
] as const
export type BucketKey = (typeof BUCKETS)[number]["key"]
const HEAVY_BUCKETS: ReadonlySet<BucketKey> = new Set([
  "le_64k",
  "le_128k",
  "le_256k",
  "gt_256k",
])

export type StackQuotaRow = {
  stackId: string
  slug: string | null
  accountName: string | null
  plan: QuotaPlan
  quota: RequestQuota
  usedWeighted: number
  /** Uso ponderado projetado para o fim do ciclo; null com menos de
   *  MIN_PROJECTION_DAYS de ciclo. */
  projectedWeighted: number | null
  projectedRaw: number | null
}

export type PlanSummary = {
  plan: QuotaPlan
  stacks: number
  raw: number
  weighted: number
  hitRawNow: number
  hitWeightedNow: number
  projectedRawHit: number
  projectedWeightedHit: number
  projectable: number
}

export type DistributionCell = { requests: number; weighted: number }

export type WeightReport = {
  /** false = migration 0079 ainda não aplicada (sem used_weighted). */
  available: boolean
  plans: PlanSummary[]
  atRisk: StackQuotaRow[]
  distribution: Record<QuotaPlan, Record<BucketKey, DistributionCell>>
  heavyShare: Record<QuotaPlan, number>
  missingTokens: { upstream: string; requests: number; missing: number }[]
  error: string | null
}

type ReportRow = {
  plan: string | null
  upstream: string
  bucket: string
  requests: number | string
  weighted: number | string
}

function quotaPlan(plan: string | null | undefined): QuotaPlan | null {
  if (plan === "Go" || plan === "VibeCoder") return "Go"
  if (plan === "Pro") return "Pro"
  return null
}

function emptyDistribution(): Record<BucketKey, DistributionCell> {
  return Object.fromEntries(
    BUCKETS.map((b) => [b.key, { requests: 0, weighted: 0 }])
  ) as Record<BucketKey, DistributionCell>
}

function project(used: number, quota: RequestQuota, now: number): number | null {
  const start = Date.parse(quota.cycleStart)
  const end = Date.parse(quota.cycleEnd)
  const elapsed = now - start
  if (!(end > start) || elapsed < MIN_PROJECTION_DAYS * DAY_MS) return null
  return Math.round((used * (end - start)) / Math.min(elapsed, end - start))
}

export async function fetchWeightReport(db: Db): Promise<WeightReport> {
  const since = new Date(Date.now() - REPORT_DAYS * DAY_MS).toISOString()
  const [quotas, stacksRes, accountsRes, reportRes] = await Promise.all([
    fetchStackRequestQuotas(db),
    db.from("stacks").select("id, account_id, slug, plan"),
    db.from("accounts").select("id, name"),
    db.rpc("request_weight_report", { p_since: since }),
  ])

  const accountName = new Map<string, string>(
    ((accountsRes.data ?? []) as { id: string; name: string | null }[]).map(
      (a) => [a.id, a.name ?? ""]
    )
  )
  const stackInfo = new Map(
    (
      (stacksRes.data ?? []) as {
        id: string
        account_id: string | null
        slug: string | null
        plan: string | null
      }[]
    ).map((s) => [s.id, s])
  )

  const now = Date.now()
  const rows: StackQuotaRow[] = []
  let available = quotas.size > 0
  for (const [stackId, quota] of quotas) {
    const plan = quotaPlan(stackInfo.get(stackId)?.plan)
    if (!plan) continue
    if (quota.usedWeighted === null) available = false
    const usedWeighted = quota.usedWeighted ?? quota.used
    const info = stackInfo.get(stackId)
    rows.push({
      stackId,
      slug: info?.slug ?? null,
      accountName: info?.account_id ? accountName.get(info.account_id) ?? null : null,
      plan,
      quota,
      usedWeighted,
      projectedWeighted: project(usedWeighted, quota, now),
      projectedRaw: project(quota.used, quota, now),
    })
  }

  const plans: PlanSummary[] = QUOTA_PLANS.map((plan) => {
    const mine = rows.filter((r) => r.plan === plan)
    const hit = (n: number | null, limit: number) => n !== null && n >= limit
    return {
      plan,
      stacks: mine.length,
      raw: mine.reduce((s, r) => s + r.quota.used, 0),
      weighted: mine.reduce((s, r) => s + r.usedWeighted, 0),
      hitRawNow: mine.filter((r) => hit(r.quota.used, r.quota.limit)).length,
      hitWeightedNow: mine.filter((r) => hit(r.usedWeighted, r.quota.limit)).length,
      projectedRawHit: mine.filter((r) => hit(r.projectedRaw, r.quota.limit)).length,
      projectedWeightedHit: mine.filter((r) =>
        hit(r.projectedWeighted, r.quota.limit)
      ).length,
      projectable: mine.filter((r) => r.projectedWeighted !== null).length,
    }
  })

  // Candidatas a request_quota_override: já em 80% do limite ou projetando
  // passar dele até o fim do ciclo.
  const atRisk = rows
    .filter(
      (r) =>
        r.usedWeighted >= r.quota.limit * 0.8 ||
        (r.projectedWeighted !== null && r.projectedWeighted >= r.quota.limit)
    )
    .sort(
      (a, b) =>
        (b.projectedWeighted ?? b.usedWeighted) / b.quota.limit -
        (a.projectedWeighted ?? a.usedWeighted) / a.quota.limit
    )

  const distribution = { Go: emptyDistribution(), Pro: emptyDistribution() }
  const missing = new Map<string, { requests: number; missing: number }>()
  for (const row of (reportRes.data ?? []) as ReportRow[]) {
    const requests = Number(row.requests)
    const m = missing.get(row.upstream) ?? { requests: 0, missing: 0 }
    m.requests += requests
    if (row.bucket === "none") m.missing += requests
    missing.set(row.upstream, m)

    const plan = quotaPlan(row.plan)
    const bucket = row.bucket as BucketKey
    if (!plan || !(bucket in distribution[plan])) continue
    distribution[plan][bucket].requests += requests
    distribution[plan][bucket].weighted += Number(row.weighted)
  }

  const heavyShare = Object.fromEntries(
    QUOTA_PLANS.map((plan) => {
      const cells = Object.entries(distribution[plan]) as [BucketKey, DistributionCell][]
      const total = cells.reduce((s, [, c]) => s + c.requests, 0)
      const heavy = cells
        .filter(([k]) => HEAVY_BUCKETS.has(k))
        .reduce((s, [, c]) => s + c.requests, 0)
      return [plan, total > 0 ? heavy / total : 0]
    })
  ) as Record<QuotaPlan, number>

  return {
    available: available && !reportRes.error,
    plans,
    atRisk,
    distribution,
    heavyShare,
    missingTokens: [...missing.entries()]
      .map(([upstream, v]) => ({ upstream, ...v }))
      .sort((a, b) => b.requests - a.requests),
    error: reportRes.error?.message ?? stacksRes.error?.message ?? null,
  }
}
