import Link from "next/link"
import { DollarSign, KeyRound, Server } from "lucide-react"

import { computeCapacity, stackWeight } from "@/lib/capacity"
import { machineDisplayStatus, reconcileMachineStatuses } from "@/lib/machines"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type {
  Machine,
  MachineEvent,
  Template,
  UsageMetric,
} from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { CapacityBar } from "@/components/machines/capacity-bar"
import { StatusBadge } from "@/components/machines/status-badge"
import { UsageDistribution } from "@/components/dashboard/usage-distribution"
import { PeriodSwitch } from "@/components/dashboard/period-switch"

export const dynamic = "force-dynamic"

const PERIOD_MS: Record<string, number | null> = {
  "24h": 24 * 60 * 60 * 1000,
  "7d": 7 * 24 * 60 * 60 * 1000,
  total: null,
}

const PERIOD_LABELS: Record<string, string> = {
  "24h": "24 horas",
  "7d": "7 dias",
  total: "Total",
}

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: Promise<{ period?: string }>
}) {
  const { period: rawPeriod } = await searchParams
  const period = rawPeriod && rawPeriod in PERIOD_MS ? rawPeriod : "24h"

  const db = createSupabaseAdmin()
  const periodMs = PERIOD_MS[period]
  const now = Date.now()
  const since = periodMs ? new Date(now - periodMs).toISOString() : null

  const [
    { data: machinesData },
    { data: templatesData },
    { data: stacksData },
    { data: eventsData },
  ] = await Promise.all([
    db.from("machines").select("*").neq("status", "terminated"),
    db.from("templates").select("*"),
    db.from("stacks").select("machine_id, usage_class").not("machine_id", "is", null),
    db
      .from("machine_events")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(8),
  ])

  // Reconcilia com o RunPod e descarta máquinas terminadas por fora.
  const reconciled = await reconcileMachineStatuses(
    (machinesData ?? []) as Machine[],
    db
  )
  const machines = reconciled.filter((m) => m.status !== "terminated")
  const templates = (templatesData ?? []) as Template[]

  const usageQuery = db.from("usage_metrics").select("*")
  const { data: usageData } = since
    ? await usageQuery.gte("window_start", since)
    : await usageQuery

  // "Rodando" só quando o vLLM já responde; antes disso, "Subindo".
  const displayStatuses = await Promise.all(machines.map(machineDisplayStatus))
  const displayStatusById = new Map(
    machines.map((m, i) => [m.id, displayStatuses[i]])
  )
  const templateById = new Map(templates.map((t) => [t.id, t]))
  const usage = (usageData ?? []) as UsageMetric[]
  const events = (eventsData ?? []) as MachineEvent[]

  // Ocupação = stacks hospedadas PONDERADAS pela classe de uso (0032),
  // não chaves ativas.
  const stacksByMachine = new Map<string, number>()
  for (const s of stacksData ?? []) {
    stacksByMachine.set(
      s.machine_id,
      (stacksByMachine.get(s.machine_id) ?? 0) + stackWeight(s.usage_class)
    )
  }

  const running = machines.filter((m) => m.status === "running")
  const capacities = machines.map((m) => {
    const tpl = m.template_id ? templateById.get(m.template_id) : undefined
    return {
      machine: m,
      cap: computeCapacity({
        vramGb: m.vram_gb,
        modelFootprintGb: tpl?.model_footprint_gb ?? 16,
        kvReserveGbPerUser: tpl?.kv_reserve_gb_per_user ?? 2,
        occupied: stacksByMachine.get(m.id) ?? 0,
        maxUsers: m.max_users,
      }),
    }
  })

  const totalSlots = capacities.reduce((s, c) => s + c.cap.slotsMax, 0)
  const usedSlots = capacities.reduce((s, c) => s + c.cap.slotsUsed, 0)
  const totalRequests = usage.reduce((s, u) => s + u.requests, 0)
  const totalTokens = usage.reduce((s, u) => s + u.tokens_in + u.tokens_out, 0)
  const totalCostPerHr = running.reduce((s, m) => s + (m.cost_per_hr ?? 0), 0)

  const machineById = new Map(machines.map((m) => [m.id, m]))
  const requestsByMachine = new Map<string, number>()
  for (const u of usage) {
    requestsByMachine.set(
      u.machine_id,
      (requestsByMachine.get(u.machine_id) ?? 0) + u.requests
    )
  }
  const donutData = [...requestsByMachine.entries()]
    .map(([id, requests]) => ({
      name: machineById.get(id)?.name ?? "removida",
      requests,
    }))
    .sort((a, b) => b.requests - a.requests)

  // Histograma de tokens ao longo do tempo. A granularidade segue o período:
  // 24h → barras por hora; 7d/total → barras por dia. Buckets contíguos
  // (incluindo zeros) para barras uniformes. Fuso fixo do Brasil (sem DST
  // desde 2019) para que "hora a hora" bata com o relógio do usuário,
  // independente do fuso do server (Railway/UTC).
  const TZ = "America/Sao_Paulo"
  const granularity = period === "24h" ? "hour" : "day"
  const partsFmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
  })
  const partsOf = (ms: number) =>
    Object.fromEntries(
      partsFmt.formatToParts(new Date(ms)).map((p) => [p.type, p.value])
    ) as Record<string, string>
  const keyOf = (ms: number) => {
    const p = partsOf(ms)
    return granularity === "hour"
      ? `${p.year}-${p.month}-${p.day}-${p.hour}`
      : `${p.year}-${p.month}-${p.day}`
  }
  const labelOf = (ms: number) => {
    const p = partsOf(ms)
    return granularity === "hour" ? `${p.hour}h` : `${p.day}/${p.month}`
  }

  const HOUR_MS = 60 * 60 * 1000
  const DAY_MS = 24 * HOUR_MS
  const step = granularity === "hour" ? HOUR_MS : DAY_MS
  let bucketCount: number
  if (granularity === "hour") {
    bucketCount = 24
  } else if (period === "7d") {
    bucketCount = 7
  } else if (usage.length === 0) {
    bucketCount = 1
  } else {
    const earliest = Math.min(
      ...usage.map((u) => new Date(u.window_start).getTime())
    )
    const days = Math.floor((now - earliest) / DAY_MS) + 1
    if (days > 30) {
      console.warn(
        `[dashboard] histograma "total" com ${days} dias — exibindo só os últimos 30`
      )
    }
    bucketCount = Math.min(30, Math.max(1, days))
  }

  const tokensByBucket = new Map<string, number>()
  for (const u of usage) {
    const k = keyOf(new Date(u.window_start).getTime())
    tokensByBucket.set(
      k,
      (tokensByBucket.get(k) ?? 0) + u.tokens_in + u.tokens_out
    )
  }
  const histogramData = Array.from({ length: bucketCount }, (_, i) => {
    const ms = now - (bucketCount - 1 - i) * step
    return { label: labelOf(ms), tokens: tokensByBucket.get(keyOf(ms)) ?? 0 }
  })

  const kpis = [
    {
      label: "Máquinas ativas",
      value: String(running.length),
      sub: `${machines.length} no total`,
      icon: Server,
    },
    {
      label: "Slots ocupados",
      value: `${usedSlots}/${totalSlots}`,
      sub: totalSlots > 0 ? `${Math.round((usedSlots / totalSlots) * 100)}% de alocação` : "sem máquinas",
      icon: KeyRound,
    },
    {
      label: "Custo por hora",
      value: `$${totalCostPerHr.toFixed(2)}`,
      sub: `~$${(totalCostPerHr * 24 * 30).toFixed(0)}/mês se 24/7`,
      icon: DollarSign,
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Visão geral da infraestrutura de LLM
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {kpis.slice(0, 2).map((kpi) => (
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
        ))}

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardDescription>Requisições</CardDescription>
            <PeriodSwitch period={period} />
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-semibold tracking-tight">
              {totalRequests.toLocaleString("pt-BR")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {totalTokens.toLocaleString("pt-BR")} tokens
            </p>
          </CardContent>
        </Card>

        {kpis.slice(2).map((kpi) => (
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
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Alocação de capacidade</CardTitle>
            <CardDescription>Slots por máquina</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            {capacities.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Nenhuma máquina.{" "}
                <Link href="/machines" className="underline">
                  Crie a primeira
                </Link>
                .
              </p>
            )}
            {capacities.map(({ machine, cap }) => (
              <div key={machine.id} className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <Link
                    href={`/machines/${machine.id}`}
                    className="text-sm font-medium hover:underline"
                  >
                    {machine.name}
                  </Link>
                  <span className="text-xs text-muted-foreground">
                    {cap.slotsUsed}/{cap.slotsMax} slots · {cap.usagePct}%
                  </span>
                </div>
                <CapacityBar used={cap.slotsUsed} max={cap.slotsMax} />
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Distribuição de uso</CardTitle>
            <CardDescription>
              Requisições por máquina ou tokens no tempo —{" "}
              {PERIOD_LABELS[period]}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <UsageDistribution
              donutData={donutData}
              histogramData={histogramData}
            />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Atividade recente</CardTitle>
          <CardDescription>Últimos eventos das máquinas</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {events.length === 0 && (
            <p className="text-sm text-muted-foreground">Nenhum evento ainda.</p>
          )}
          {events.map((e) => (
            <div key={e.id} className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-3">
                <span className="size-2 rounded-full bg-emerald-500" />
                <p className="text-sm">{e.message}</p>
              </div>
              <div className="flex items-center gap-3">
                <Badge variant="outline">{e.type}</Badge>
                <span className="text-xs text-muted-foreground">
                  {new Date(e.created_at).toLocaleString("pt-BR")}
                </span>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {machines.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Máquinas</CardTitle>
            <CardDescription>Acesso rápido</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {machines.map((m) => (
              <Link
                key={m.id}
                href={`/machines/${m.id}`}
                className="flex items-center justify-between rounded-lg border p-3 transition-colors hover:bg-accent"
              >
                <div>
                  <p className="text-sm font-medium">{m.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {m.gpu_type} · {m.model_name}
                  </p>
                </div>
                <StatusBadge status={displayStatusById.get(m.id) ?? m.status} />
              </Link>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
