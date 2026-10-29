import Link from "next/link"
import { Cpu, DollarSign, PiggyBank, Timer, Wallet, Waypoints, Zap } from "lucide-react"

import {
  bucketizeCost,
  formatUsd,
  summarizeCost,
  type Granularity,
} from "@/lib/billing"
import { formatTokens } from "@/lib/crm"
import { getOpenRouterCredits } from "@/lib/openrouter"
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

import { loadCostData, loadOpenRouterCost } from "./queries"

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
  // Object.hasOwn e não `in`: `in` alcança o protótipo, então ?period=constructor
  // passava na validação, fazia periodMs virar uma função e a janela virar NaN —
  // e como NaN falha toda comparação (inclusive os guardas `<= 0` de
  // overlapHours), o gráfico e a tabela inteiros renderizavam NaN.
  const period =
    rawPeriod && Object.hasOwn(PERIOD_MS, rawPeriod) ? rawPeriod : "24h"
  const hourAllowed = HOUR_ALLOWED.has(period)
  const granularity: Granularity =
    hourAllowed && rawBucket !== "day" ? "hour" : "day"

  const [cost, openRouter, credits] = await Promise.all([
    loadCostData(PERIOD_MS[period]),
    loadOpenRouterCost(PERIOD_MS[period]),
    getOpenRouterCredits(),
  ])
  const { machines, intervals, to } = cost
  // "total" começa no registro mais antigo das DUAS fontes: o OpenRouter pode
  // ter cobrança de antes do primeiro intervalo de máquina.
  const from =
    period === "total" && openRouter.earliest !== null
      ? Math.min(cost.from, openRouter.earliest)
      : cost.from
  const dataIncomplete = cost.dataIncomplete || openRouter.dataIncomplete

  const summary = summarizeCost(intervals, machines, from, to)
  const buckets = bucketizeCost(intervals, from, to, granularity, openRouter.charges)
  const totalSpent = summary.spent + openRouter.totalUsd

  // Abaixo disso o crédito acaba em dias e as requisições passam a levar 402
  // (o cliente recebe 503) — o alerta tem que aparecer antes, não depois.
  const lowCredit =
    credits.ok &&
    (credits.remaining < 10 ||
      (credits.totalCredits > 0 && credits.remaining / credits.totalCredits < 0.2))

  const running = machines.filter((m) => m.status === "running")
  const costNow = running.reduce((s, m) => s + (m.cost_per_hr ?? 0), 0)

  const totals = [
    {
      label: "Gasto total no período",
      value: formatUsd(totalSpent),
      sub: "RunPod + OpenRouter",
      icon: DollarSign,
    },
    {
      label: "RunPod (GPU)",
      value: formatUsd(summary.spent),
      sub: `${summary.billableHours.toFixed(1)}h de máquina ligada`,
      icon: Cpu,
    },
    {
      label: "OpenRouter",
      value: formatUsd(openRouter.totalUsd),
      sub: `${openRouter.requests.toLocaleString("pt-BR")} requisição(ões) cobrada(s)`,
      icon: Waypoints,
    },
    {
      label: "Saldo no OpenRouter",
      value: credits.ok ? formatUsd(credits.remaining) : "—",
      sub: credits.ok
        ? `de ${formatUsd(credits.totalCredits)} em créditos`
        : credits.reason === "missing_key"
          ? "configure OPENROUTER_MANAGEMENT_KEY"
          : "OpenRouter indisponível agora",
      icon: Wallet,
    },
  ]

  const kpis = [
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
  const kpiCards = (items: typeof totals) =>
    items.map((kpi) => (
      <Card key={kpi.label}>
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <CardDescription>{kpi.label}</CardDescription>
          <kpi.icon className="size-4 text-muted-foreground" />
        </CardHeader>
        <CardContent>
          <p className="text-3xl font-semibold tracking-tight">{kpi.value}</p>
          <p className="mt-1 text-xs text-muted-foreground">{kpi.sub}</p>
        </CardContent>
      </Card>
    ))

  return (
    <>
      {dataIncomplete && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Os números abaixo estão incompletos: a leitura do histórico de custo
          falhou ou passou do teto de paginação. Recarregue a página; se
          persistir, o total está subestimado.
        </p>
      )}
      {lowCredit && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Saldo do OpenRouter baixo: {formatUsd(credits.remaining)}. Quando ele
          acaba, o OpenRouter responde 402 e o cliente recebe 503
          {openRouter.outOfCredit > 0 &&
            ` — ${openRouter.outOfCredit.toLocaleString("pt-BR")} requisição(ões) já levaram 402 no período`}
          . Recarregue os créditos em openrouter.ai.
        </p>
      )}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{kpiCards(totals)}</div>

      <div className="flex flex-col gap-3">
        <h2 className="text-sm font-medium text-muted-foreground">RunPod</h2>
        <div className="grid gap-4 sm:grid-cols-3">{kpiCards(kpis)}</div>
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-col gap-1.5">
            <CardTitle>Gasto ao longo do tempo</CardTitle>
            <CardDescription>
              RunPod e OpenRouter, e quanto as máquinas custariam ligadas 24/7 —{" "}
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
          <CardTitle>RunPod · por máquina</CardTitle>
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
                  <TableHead className="text-right!">$/h</TableHead>
                  <TableHead className="text-right!">Ligada</TableHead>
                  <TableHead className="text-right!">Uptime</TableHead>
                  <TableHead className="text-right!">Gasto</TableHead>
                  <TableHead className="text-right!">Se 24/7</TableHead>
                  <TableHead className="text-right!">Economia</TableHead>
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

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>OpenRouter · por modelo</CardTitle>
            <CardDescription>
              Custo que o OpenRouter cobrou em cada resposta — {PERIOD_LABELS[period]}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {openRouter.byModel.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Nenhuma requisição cobrada pelo OpenRouter no período.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Modelo</TableHead>
                    <TableHead className="text-right!">Requisições</TableHead>
                    <TableHead className="text-right!">Tokens</TableHead>
                    <TableHead className="text-right!">Gasto</TableHead>
                    <TableHead className="text-right!">%</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {openRouter.byModel.map((row) => (
                    <TableRow key={row.model}>
                      <TableCell className="max-w-56 truncate font-mono text-xs">
                        {row.model}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {row.requests.toLocaleString("pt-BR")}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                        {formatTokens(row.tokens)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {formatUsd(row.costUsd)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                        {sharePct(row.costUsd, openRouter.totalUsd)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>OpenRouter · por cliente</CardTitle>
            <CardDescription>
              Quem gerou o gasto repassado ao OpenRouter — {PERIOD_LABELS[period]}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {openRouter.byAccount.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Nenhuma requisição cobrada pelo OpenRouter no período.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Conta</TableHead>
                    <TableHead className="text-right!">Requisições</TableHead>
                    <TableHead className="text-right!">Gasto</TableHead>
                    <TableHead className="text-right!">%</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {openRouter.byAccount.map((row) => (
                    <TableRow key={row.accountId ?? "sem-conta"}>
                      <TableCell className="max-w-56 truncate font-medium">
                        {row.accountId ? (
                          <Link href={`/crm/${row.accountId}`} className="hover:underline">
                            {row.name ?? row.accountId}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">sem conta</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {row.requests.toLocaleString("pt-BR")}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {formatUsd(row.costUsd)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums text-muted-foreground">
                        {sharePct(row.costUsd, openRouter.totalUsd)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  )
}

function sharePct(part: number, total: number): string {
  if (total <= 0) return "—"
  const pct = (part / total) * 100
  return pct < 1 ? "<1%" : `${Math.round(pct)}%`
}
