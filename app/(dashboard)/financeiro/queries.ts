// Leitura do gasto com máquinas: compartilhada pela página Financeiro e pela
// rota MCP (app/api/mcp/[secret]), para as duas nunca divergirem.
//
// Sem checagem de acesso — a página é barrada pelo layout do dashboard e a rota
// MCP pelo segredo na URL.

import { costWindow, type RuntimeInterval } from "@/lib/billing"
import { fetchAll, type PagedQuery } from "@/lib/paged-query"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Machine } from "@/lib/types"

const DAY_MS = 24 * 60 * 60 * 1000

export type CostData = {
  machines: Machine[]
  intervals: RuntimeInterval[]
  dataIncomplete: boolean
  from: number
  to: number
}

/** periodMs null = todo o histórico. */
export async function loadCostData(periodMs: number | null): Promise<CostData> {
  const db = createSupabaseAdmin()
  const { from: windowFrom, to } = costWindow(periodMs)

  // Máquinas terminadas entram: elas gastaram dinheiro dentro da janela e a
  // linha da tabela precisa do nome. Quem decide o que conta é o status do
  // intervalo, não o status atual da máquina.
  // Paginadas (lib/paged-query.ts): o teto de 1000 linhas do PostgREST vale
  // para qualquer select, e o trigger de intervalos grava uma linha a cada
  // mudança de status OU de cost_per_hr — passando de 1000 na janela, só as
  // linhas mais antigas chegavam e o gasto recente sumia do relatório (o CRM
  // já pagina a mesma tabela pelo mesmo motivo).
  const [machinesResult, intervalsResult] = await Promise.all([
    fetchAll<Machine>(
      () => db.from("machines").select("*").order("id") as unknown as PagedQuery
    ),
    fetchAll<RuntimeInterval>(() => {
      const q = db.from("machine_runtime_intervals").select("*").order("id")
      return (
        windowFrom
          ? q
              .lte("started_at", new Date(to).toISOString())
              .or(`ended_at.is.null,ended_at.gte.${new Date(windowFrom).toISOString()}`)
          : q
      ) as unknown as PagedQuery
    }),
  ])

  const machines = machinesResult.rows
  const intervals = intervalsResult.rows

  // "total" começa no primeiro registro que existe (ordenação por id, então
  // o mais antigo é o menor started_at, não o primeiro da lista); sem
  // histórico, uma janela de 24h só para o gráfico não nascer vazio.
  const earliestStart = intervals.reduce<number | null>((min, i) => {
    const t = new Date(i.started_at).getTime()
    return min === null || t < min ? t : min
  }, null)

  return {
    machines,
    intervals,
    dataIncomplete:
      machinesResult.failed ||
      machinesResult.truncated ||
      intervalsResult.failed ||
      intervalsResult.truncated,
    from: windowFrom ?? earliestStart ?? to - DAY_MS,
    to,
  }
}
