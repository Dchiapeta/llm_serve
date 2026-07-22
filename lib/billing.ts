import type { Machine } from "./types"

// Histórico de estado por máquina (migration 0034). Um registro por
// "status + custo" vigente; contíguos desde a criação da máquina.
export type RuntimeInterval = {
  id: string
  machine_id: string
  status: Machine["status"]
  cost_per_hr: number | null
  started_at: string
  ended_at: string | null
}

// Estados em que o pod está ALOCADO no RunPod e portanto cobrando GPU.
// "creating" cobra desde a alocação (antes do vLLM subir) e "error" costuma
// ser pod vivo com processo morto — pior caso, cobra igual. "stopped" só
// cobra storage, que cost_per_hr (preço da GPU) não representa → conta zero.
// "terminated" fica fora dos DOIS lados da conta: nem gasto, nem baseline.
const BILLABLE_STATUSES: ReadonlySet<string> = new Set([
  "running",
  "creating",
  "error",
])

// Um intervalo "terminated" existe só para fechar a linha do tempo da máquina;
// incluí-lo no baseline diria que uma máquina apagada há um mês "poderia estar
// ligada 24/7", inflando a economia com tempo que nem existia.
const LIVE_STATUSES = (status: string) => status !== "terminated"

export type Granularity = "hour" | "day"

const HOUR_MS = 60 * 60 * 1000
const DAY_MS = 24 * HOUR_MS

// Fuso fixo do Brasil (sem DST desde 2019) — o server pode rodar em UTC
// (Railway) e "hora a hora" precisa bater com o relógio do usuário. Mesma
// escolha do histograma do dashboard.
const TZ = "America/Sao_Paulo"
const TZ_OFFSET_MS = -3 * HOUR_MS

const labelFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  hourCycle: "h23",
})

function labelOf(ms: number, granularity: Granularity) {
  const parts = Object.fromEntries(
    labelFmt.formatToParts(new Date(ms)).map((p) => [p.type, p.value])
  ) as Record<string, string>
  return granularity === "hour"
    ? `${parts.hour}h`
    : `${parts.day}/${parts.month}`
}

// Trunca para a fronteira do bucket NO FUSO DE SÃO PAULO — sem isso o bucket
// "dia" começaria às 21h do dia anterior para quem olha o painel.
function floorToBucket(ms: number, step: number) {
  return Math.floor((ms + TZ_OFFSET_MS) / step) * step - TZ_OFFSET_MS
}

// Sobreposição entre o intervalo e [from, to), em horas. Intervalo vigente
// (ended_at null) é cortado em `to`.
function overlapHours(
  interval: RuntimeInterval,
  from: number,
  to: number
): number {
  const start = Math.max(new Date(interval.started_at).getTime(), from)
  const end = Math.min(
    interval.ended_at ? new Date(interval.ended_at).getTime() : to,
    to
  )
  return end <= start ? 0 : (end - start) / HOUR_MS
}

export type MachineCostRow = {
  machine: Machine | undefined
  machineId: string
  costPerHr: number | null
  /** horas em que o pod esteve alocado (cobrando GPU) */
  billableHours: number
  /** horas de existência da máquina dentro da janela (ligada ou não) */
  lifetimeHours: number
  spent: number
  baseline: number
  saved: number
  uptimePct: number
}

export type CostSummary = {
  spent: number
  baseline: number
  saved: number
  savedPct: number
  billableHours: number
  lifetimeHours: number
  /** máquinas com gasto no período mas sem cost_per_hr conhecido */
  unpricedMachines: number
  byMachine: MachineCostRow[]
}

// Gasto real vs. custo hipotético 24/7 na janela [from, to).
//
// O baseline sai dos PRÓPRIOS intervalos (tempo de vida na janela), não de
// "horas da janela × nº de máquinas": é o que impede uma máquina criada há 10
// minutos de entrar na conta como se pudesse ter ficado ligada 7 dias.
export function summarizeCost(
  intervals: RuntimeInterval[],
  machines: Machine[],
  from: number,
  to: number
): CostSummary {
  const machineById = new Map(machines.map((m) => [m.id, m]))
  const rows = new Map<string, MachineCostRow>()

  for (const iv of intervals) {
    if (!LIVE_STATUSES(iv.status)) continue
    const hours = overlapHours(iv, from, to)
    if (hours <= 0) continue

    const cost = iv.cost_per_hr ?? 0
    let row = rows.get(iv.machine_id)
    if (!row) {
      row = {
        machine: machineById.get(iv.machine_id),
        machineId: iv.machine_id,
        costPerHr: iv.cost_per_hr,
        billableHours: 0,
        lifetimeHours: 0,
        spent: 0,
        baseline: 0,
        saved: 0,
        uptimePct: 0,
      }
      rows.set(iv.machine_id, row)
    }
    // preço vigente = o do intervalo mais recente que tocou a janela
    if (iv.cost_per_hr !== null) row.costPerHr = iv.cost_per_hr

    row.lifetimeHours += hours
    row.baseline += hours * cost
    if (BILLABLE_STATUSES.has(iv.status)) {
      row.billableHours += hours
      row.spent += hours * cost
    }
  }

  const byMachine = [...rows.values()]
  for (const row of byMachine) {
    row.saved = row.baseline - row.spent
    row.uptimePct =
      row.lifetimeHours > 0
        ? Math.round((row.billableHours / row.lifetimeHours) * 100)
        : 0
  }
  byMachine.sort((a, b) => b.spent - a.spent)

  const spent = byMachine.reduce((s, r) => s + r.spent, 0)
  const baseline = byMachine.reduce((s, r) => s + r.baseline, 0)

  return {
    spent,
    baseline,
    saved: baseline - spent,
    savedPct: baseline > 0 ? Math.round(((baseline - spent) / baseline) * 100) : 0,
    billableHours: byMachine.reduce((s, r) => s + r.billableHours, 0),
    lifetimeHours: byMachine.reduce((s, r) => s + r.lifetimeHours, 0),
    unpricedMachines: byMachine.filter(
      (r) => r.costPerHr === null && r.billableHours > 0
    ).length,
    byMachine,
  }
}

// Janela do relatório. Vive aqui, e não no server component, porque Date.now()
// é impuro e o lint do React (com razão) proíbe chamá-lo durante o render.
// `from` null = "total": a borda esquerda só é conhecida depois de saber qual é
// o registro mais antigo.
export function costWindow(periodMs: number | null): {
  from: number | null
  to: number
} {
  const to = Date.now()
  return { from: periodMs ? to - periodMs : null, to }
}

export type CostBucket = {
  label: string
  spent: number
  baseline: number
}

// Série temporal do gasto: buckets contíguos (incluindo zeros, para barras
// uniformes) cobrindo [from, to). Cada intervalo é repartido entre os buckets
// que ele atravessa, proporcional ao tempo em cada um.
export function bucketizeCost(
  intervals: RuntimeInterval[],
  from: number,
  to: number,
  granularity: Granularity
): CostBucket[] {
  const step = granularity === "hour" ? HOUR_MS : DAY_MS
  const origin = floorToBucket(from, step)
  const count = Math.max(1, Math.ceil((to - origin) / step))

  const buckets: CostBucket[] = Array.from({ length: count }, (_, i) => ({
    label: labelOf(origin + i * step, granularity),
    spent: 0,
    baseline: 0,
  }))

  for (const iv of intervals) {
    if (!LIVE_STATUSES(iv.status)) continue
    const cost = iv.cost_per_hr ?? 0
    if (cost === 0) continue

    const start = Math.max(new Date(iv.started_at).getTime(), from)
    const end = Math.min(
      iv.ended_at ? new Date(iv.ended_at).getTime() : to,
      to
    )
    if (end <= start) continue

    const billable = BILLABLE_STATUSES.has(iv.status)
    const firstIdx = Math.max(0, Math.floor((start - origin) / step))
    const lastIdx = Math.min(count - 1, Math.floor((end - origin) / step))

    for (let i = firstIdx; i <= lastIdx; i++) {
      const bucketStart = origin + i * step
      const hours =
        (Math.min(end, bucketStart + step) - Math.max(start, bucketStart)) /
        HOUR_MS
      if (hours <= 0) continue
      buckets[i].baseline += hours * cost
      if (billable) buckets[i].spent += hours * cost
    }
  }

  return buckets
}

export function formatUsd(value: number) {
  return `$${value.toFixed(2)}`
}
