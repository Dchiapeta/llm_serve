// Leitura do gasto com máquinas (RunPod) e com repasse ao OpenRouter:
// compartilhada pela página Financeiro e pela rota MCP (app/api/mcp/[secret]),
// para as duas nunca divergirem.
//
// Sem checagem de acesso — a página é barrada pelo layout do dashboard e a rota
// MCP pelo segredo na URL.

import { costWindow, type OpenRouterCharge, type RuntimeInterval } from "@/lib/billing"
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

// ---------- OpenRouter ----------

type OpenRouterRequestRow = OpenRouterCharge & {
  id: string
  model: string | null
  account_id: string | null
  tokens_in: number | null
  tokens_out: number | null
}

export type OpenRouterModelCost = {
  model: string
  requests: number
  tokens: number
  costUsd: number
}

export type OpenRouterAccountCost = {
  accountId: string | null
  name: string | null
  requests: number
  costUsd: number
}

export type OpenRouterCostData = {
  charges: OpenRouterCharge[]
  totalUsd: number
  requests: number
  byModel: OpenRouterModelCost[]
  byAccount: OpenRouterAccountCost[]
  /** Requisições que levaram 402 do OpenRouter: crédito da conta acabou. */
  outOfCredit: number
  /** Primeira cobrança da janela — a borda esquerda do "total". */
  earliest: number | null
  dataIncomplete: boolean
}

/**
 * Gasto com o OpenRouter a partir de gateway_requests.cost_usd (0071): o custo
 * que o próprio OpenRouter devolve em cada resposta, gravado pelo gateway.
 *
 * Só linhas com custo: as sem custo são todas erro sem tokens (402, 503, 499,
 * 400 — o OpenRouter não cobrou nada). Paginado como o resto (fetchAll); passa
 * de 50 mil linhas na janela e o aviso de incompleto acende — aí é hora de
 * agregar o custo em usage_metrics, como o trigger da 0075 já faz com tokens.
 */
export async function loadOpenRouterCost(
  periodMs: number | null
): Promise<OpenRouterCostData> {
  const db = createSupabaseAdmin()
  const { from: windowFrom, to } = costWindow(periodMs)
  const fromIso = windowFrom ? new Date(windowFrom).toISOString() : null
  const toIso = new Date(to).toISOString()

  const [result, outOfCreditRes] = await Promise.all([
    fetchAll<OpenRouterRequestRow>(() => {
      const q = db
        .from("gateway_requests")
        .select("id, created_at, cost_usd, model, account_id, tokens_in, tokens_out")
        .eq("upstream", "openrouter")
        .not("cost_usd", "is", null)
        .lte("created_at", toIso)
        .order("id")
      return (fromIso ? q.gte("created_at", fromIso) : q) as unknown as PagedQuery
    }),
    (() => {
      const q = db
        .from("gateway_requests")
        .select("id", { count: "exact", head: true })
        .eq("upstream", "openrouter")
        .eq("status_code", 402)
        .lte("created_at", toIso)
      return fromIso ? q.gte("created_at", fromIso) : q
    })(),
  ])

  // numeric chega como string pelo PostgREST
  const rows = result.rows.map((r) => ({ ...r, cost_usd: Number(r.cost_usd) }))

  const models = new Map<string, OpenRouterModelCost>()
  const accounts = new Map<string | null, OpenRouterAccountCost>()
  let totalUsd = 0
  let earliest: number | null = null
  for (const r of rows) {
    totalUsd += r.cost_usd
    const t = new Date(r.created_at).getTime()
    if (earliest === null || t < earliest) earliest = t

    const model = r.model ?? "desconhecido"
    const m = models.get(model) ?? { model, requests: 0, tokens: 0, costUsd: 0 }
    m.requests += 1
    m.tokens += (r.tokens_in ?? 0) + (r.tokens_out ?? 0)
    m.costUsd += r.cost_usd
    models.set(model, m)

    const a = accounts.get(r.account_id) ?? {
      accountId: r.account_id,
      name: null,
      requests: 0,
      costUsd: 0,
    }
    a.requests += 1
    a.costUsd += r.cost_usd
    accounts.set(r.account_id, a)
  }

  const accountIds = [...accounts.keys()].filter((id): id is string => id !== null)
  let namesFailed = false
  if (accountIds.length > 0) {
    const { data, error } = await db.from("accounts").select("id, name").in("id", accountIds)
    namesFailed = Boolean(error)
    for (const acc of data ?? []) {
      const a = accounts.get(acc.id)
      if (a) a.name = acc.name
    }
  }

  const byCost = <T extends { costUsd: number }>(a: T, b: T) => b.costUsd - a.costUsd

  return {
    charges: rows,
    totalUsd,
    requests: rows.length,
    byModel: [...models.values()].sort(byCost),
    byAccount: [...accounts.values()].sort(byCost),
    outOfCredit: outOfCreditRes.count ?? 0,
    earliest,
    dataIncomplete:
      result.failed || result.truncated || Boolean(outOfCreditRes.error) || namesFailed,
  }
}
