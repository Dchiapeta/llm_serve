import Link from "next/link"
import { Activity, DollarSign, KeyRound, Server } from "lucide-react"

import { computeCapacity } from "@/lib/capacity"
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
import { PollMetricsButton } from "@/components/dashboard/poll-metrics-button"
import { UsageDonut } from "@/components/dashboard/usage-donut"

export const dynamic = "force-dynamic"

export default async function DashboardPage() {
  const db = createSupabaseAdmin()
  const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString()

  const [
    { data: machinesData },
    { data: templatesData },
    { data: keysData },
    { data: usageData },
    { data: eventsData },
  ] = await Promise.all([
    db.from("machines").select("*").neq("status", "terminated"),
    db.from("templates").select("*"),
    db.from("api_keys").select("machine_id, status").eq("status", "active"),
    db.from("usage_metrics").select("*").gte("window_start", since),
    db
      .from("machine_events")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(8),
  ])

  const machines = (machinesData ?? []) as Machine[]
  const templates = (templatesData ?? []) as Template[]
  const templateById = new Map(templates.map((t) => [t.id, t]))
  const usage = (usageData ?? []) as UsageMetric[]
  const events = (eventsData ?? []) as MachineEvent[]

  const activeKeysByMachine = new Map<string, number>()
  for (const k of keysData ?? []) {
    activeKeysByMachine.set(
      k.machine_id,
      (activeKeysByMachine.get(k.machine_id) ?? 0) + 1
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
        activeKeys: activeKeysByMachine.get(m.id) ?? 0,
      }),
    }
  })

  const totalSlots = capacities.reduce((s, c) => s + c.cap.slotsMax, 0)
  const usedSlots = capacities.reduce((s, c) => s + c.cap.slotsUsed, 0)
  const totalRequests24h = usage.reduce((s, u) => s + u.requests, 0)
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
      label: "Requisições (24h)",
      value: totalRequests24h.toLocaleString("pt-BR"),
      sub: `${usage
        .reduce((s, u) => s + u.tokens_in + u.tokens_out, 0)
        .toLocaleString("pt-BR")} tokens`,
      icon: Activity,
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <p className="text-sm text-muted-foreground">
            Visão geral da infraestrutura de LLM
          </p>
        </div>
        <PollMetricsButton />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {kpis.map((kpi) => (
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
            <CardDescription>Requisições por máquina nas últimas 24h</CardDescription>
          </CardHeader>
          <CardContent>
            <UsageDonut data={donutData} />
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
                <StatusBadge status={m.status} />
              </Link>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
