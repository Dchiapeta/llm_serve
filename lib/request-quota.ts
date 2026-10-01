// Cota mensal de requisições por stack (migration 0076): Go 4.000, Pro 10.000,
// no ciclo que começa no dia de stacks.purchase_date. O limite, o ciclo e a
// contagem vêm do banco — a mesma função que o gateway usa para cortar com
// 429 —, então o número mostrado aqui e o corte nunca discordam.
//
// Só requisições gastam a cota. Os tokens que o painel mostra ao lado são
// informativos; nenhum caminho do produto cobra ou corta por token.

import type { createSupabaseAdmin } from "@/lib/supabase/server"

export type RequestQuota = {
  limit: number
  /** Requisições registradas no ciclo, de qualquer rota e status. */
  used: number
  cycleStart: string
  cycleEnd: string
}

type QuotaRow = {
  stack_id: string
  quota_limit: number | null
  used: number | string
  cycle_start: string
  cycle_end: string
}

/**
 * Cota de todas as stacks que têm teto (LLM Go/Pro). Stack ausente do mapa =
 * sem cota (imagem, Max, Enterprise).
 *
 * Falha devolve mapa vazio em vez de derrubar a página: a coluna é aditiva, e
 * "sem cota" é o estado de antes da 0076 ser aplicada — o painel pode subir
 * antes da migration sem quebrar a tela de Stacks.
 */
export async function fetchStackRequestQuotas(
  db: ReturnType<typeof createSupabaseAdmin>
): Promise<Map<string, RequestQuota>> {
  const { data, error } = await db.rpc("stack_request_quotas")
  if (error) {
    console.error(`stack_request_quotas: ${error.message}`)
    return new Map()
  }
  const quotas = new Map<string, RequestQuota>()
  for (const row of (data ?? []) as QuotaRow[]) {
    if (row.quota_limit === null) continue
    quotas.set(row.stack_id, {
      limit: Number(row.quota_limit),
      used: Number(row.used),
      cycleStart: row.cycle_start,
      cycleEnd: row.cycle_end,
    })
  }
  return quotas
}

/**
 * Data de um limite de ciclo. UTC porque o ciclo vira à meia-noite UTC do dia
 * da compra; no fuso de São Paulo cairia na véspera e o painel mostraria um
 * dia diferente de purchase_date.
 */
export function formatCycleDate(iso: string): string {
  return new Date(iso).toLocaleDateString("pt-BR", { timeZone: "UTC" })
}

export function quotaRatio(quota: RequestQuota): number {
  return quota.limit > 0 ? quota.used / quota.limit : 1
}
