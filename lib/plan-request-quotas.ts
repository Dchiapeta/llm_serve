// Limite mensal de requisições de cada plano (migration 0080). A tabela é a
// fonte única: stack_request_quota corta com ela no gateway, e o site do
// TryStac monta os textos de planos (FAQ, cards, tabela comparativa) com estes
// números. Mudar aqui muda os dois lados — o site em até 5 minutos (cache do
// TryStac), o gateway em até 1 minuto (cache da cota por stack).

import type { createSupabaseAdmin } from "@/lib/supabase/server"

export const LIMITED_PLANS = ["Go", "Pro"] as const
export type LimitedPlan = (typeof LIMITED_PLANS)[number]

export type PlanRequestQuota = {
  plan: LimitedPlan
  limit: number
  updatedAt: string
  updatedBy: string | null
}

export function isLimitedPlan(plan: unknown): plan is LimitedPlan {
  return LIMITED_PLANS.includes(plan as LimitedPlan)
}

/**
 * Limites por plano, na ordem de LIMITED_PLANS. `available: false` = tabela
 * ainda não existe (0080 por aplicar) — a página esconde a edição em vez de
 * quebrar.
 */
export async function fetchPlanRequestQuotas(
  db: ReturnType<typeof createSupabaseAdmin>
): Promise<{ available: boolean; quotas: PlanRequestQuota[]; error: string | null }> {
  const { data, error } = await db
    .from("plan_request_quotas")
    .select("plan, quota_limit, updated_at, updated_by")
  if (error) return { available: false, quotas: [], error: error.message }

  const byPlan = new Map(
    (
      (data ?? []) as {
        plan: string
        quota_limit: number
        updated_at: string
        updated_by: string | null
      }[]
    ).map((row) => [row.plan, row])
  )
  const quotas = LIMITED_PLANS.flatMap((plan) => {
    const row = byPlan.get(plan)
    return row
      ? [
          {
            plan,
            limit: Number(row.quota_limit),
            updatedAt: row.updated_at,
            updatedBy: row.updated_by,
          },
        ]
      : []
  })
  return { available: true, quotas, error: null }
}
