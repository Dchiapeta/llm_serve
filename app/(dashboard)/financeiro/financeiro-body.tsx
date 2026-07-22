import Link from "next/link"
import { DollarSign, PiggyBank, Timer, Zap } from "lucide-react"

import {
  bucketizeCost,
  costWindow,
  formatUsd,
  summarizeCost,
  type Granularity,
  type RuntimeInterval,
} from "@/lib/billing"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Machine } from "@/lib/types"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { CostChart } from "@/components/dashboard/cost-chart"
import { GranularitySwitch } from "@/components/dashboard/granularity-switch"
import { PeriodSwitch } from "@/components/dashboard/period-switch"

const PERIOD_OPTIONS = [
  { value: "24h", label: "24 horas" },
  { value: "7d", label: "7 dias" },
  { value: "30d", label: "30 dias" },
  { value: "total", label: "Total" },
] as const

const PERIOD_MS: Record<string, number | null> = {
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  "30d": 30 * 24 * 60 * 60 * 1000,
  total: null,
}

const PERIOD_LABELS: Record<string, string> = {
  "24h": "últimas 24 horas",
  "7d": "últimos 7 dias",
  "30d": "últimos 30 dias",
  total: "todo o histórico",
}

// Períodos longos por hora dariam 720+ barras — ilegível. Nesses casos a
// granularidade é forçada para dia e o toggle fica desabilitado.
const HOUR_ALLOWED = new Set(["24h", "7d"])

export async function FinanceiroBody({
  searchParamsPromise,
}: {
  searchParamsPromise: Promise<{ period?: string; bucket?: string }>
}) {
  const { period: rawPeriod, bucket: rawBucket } = await searchParamsPromise
  const period = rawPeriod && rawPeriod in PERIOD_MS ? rawPeriod : "24h"
  const hourAllowed = HOUR_ALLOWED.has(period)
  const granularity: Granularity =
    hourAllowed && rawBucket !== "day" ? "hour" : "day"

  const db = createSupabaseAdmin()
  const periodMs = PERIOD_MS[period]
  const { from: windowFrom, to } = costWindow(periodMs)

  // Máquinas terminadas entram: elas gastaram dinheiro dentro da janela e a
  // linha da tabela precisa do nome. Quem decide o que conta é o status do
  // intervalo, não o status atual da máquina.
  const machinesQuery = db.from("machines").select("*")

  const intervalsQuery = db
    .from("machine_runtime_intervals")
    .select("*")
    .order("started_at", { ascending: true })

  const [{ data: machinesData }, { data: intervalsData }] = await Promise.all([
    machinesQuery,
    windowFrom
      ? intervalsQuery
          .lte("started_at", new Date(to).toISOString())
          .or(`ended_at.is.null,ended_at.gte.${new Date(windowFrom).toISOString()}`)
      : intervalsQuery,
  ])

  const machines = (machinesData ?? []) as Machine[]
  const intervals = (intervalsData ?? []) as RuntimeInterval[]

  // "total" começa no primeiro registro que existe (a lista vem ordenada);
  // sem histórico, uma janela de 24h só para o gráfico não nascer vazio.
  const from =
    windowFrom ??
    (intervals.length > 0
      ? new Date(intervals[0].started_at).getTime()
      : to - PERIOD_MS["24h"]!)

  const summary = summarizeCost(intervals, machines, from, to)
  const buckets = bucketizeCost(intervals, from, to, granularity)

  const running = machines.filter((m) => m.status === "running")
  const costNow = running.reduce((s, m) => s + (m.cost_per_hr ?? 0), 0)

  const kpis = [
    {
      label: "Gasto no período",
      value: formatUsd(summary.spent),
      sub: `${summary.billableHours.toFixed(1)}h de máquina ligada`,
      icon: DollarSign,
    },
    {
      label: "Se ficasse 24/7",
      value: formatUsd(summary.baseline),
      sub: `${summary.lifetimeHours.toFixed(1)}h de máquina existente`,
      icon: Timer,
    },
    {
      label: "Economia",
      value: formatUsd(summary.saved),
      sub: `${summary.savedPct}% a menos que ligado direto`,
      icon: PiggyBank,
    },
    {
      label: "Custo por hora agora",
      value: formatUsd(costNow),
      sub: `${running.length} máquina(s) ligada(s)`,
      icon: Zap,
    },
  ]

  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {kpis.map((kpi) => (
          <Card key={kpi.label}>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardDescription>{kpi.label}</CardDescription>
              <kpi.icon className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-semibold tracking-tight">
                {kpi.value}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{kpi.sub}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-col gap-1.5">
            <CardTitle>Gasto ao longo do tempo</CardTitle>
            <CardDescription>
              Custo real vs. custo se as máquinas ficassem ligadas —{" "}
              {PERIOD_LABELS[period]}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <GranularitySwitch
              granularity={granularity}
              hourDisabled={!hourAllowed}
            />
            <PeriodSwitch period={period} options={PERIOD_OPTIONS} />
          </div>
        </CardHeader>
        <CardContent>
          <CostChart data={buckets} />
          {summary.unpricedMachines > 0 && (
            <p className="mt-4 text-xs text-muted-foreground">
              {summary.unpricedMachines} máquina(s) sem preço por hora conhecido
              contam como $0 — o RunPod só informa o custo depois que o pod é
              alocado.
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Por máquina</CardTitle>
          <CardDescription>
            Quanto cada máquina custou e quanto o liga/desliga poupou
          </CardDescription>
        </CardHeader>
        <CardContent>
          {summary.byMachine.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nenhum registro de custo no período.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Máquina</TableHead>
                  <TableHead>GPU</TableHead>
                  <TableHead className="text-right">$/h</TableHead>
                  <TableHead className="text-right">Ligada</TableHead>
                  <TableHead className="text-right">Uptime</TableHead>
                  <TableHead className="text-right">Gasto</TableHead>
                  <TableHead className="text-right">Se 24/7</TableHead>
                  <TableHead className="text-right">Economia</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {summary.byMachine.map((row) => (
                  <TableRow key={row.machineId}>
                    <TableCell className="font-medium">
                      {row.machine ? (
                        <Link
                          href={`/machines/${row.machineId}`}
                          className="hover:underline"
                        >
                          {row.machine.name}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">removida</span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {row.machine?.gpu_type ?? "—"}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {row.costPerHr === null ? "—" : formatUsd(row.costPerHr)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {row.billableHours.toFixed(1)}h
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {row.uptimePct}%
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {formatUsd(row.spent)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                      {formatUsd(row.baseline)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {formatUsd(row.saved)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </>
  )
}
