import { KeyRound, Server, ServerCog, Users } from "lucide-react"
import Link from "next/link"

import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Account, ApiKey, LoraAdapter, Machine, RoutingState } from "@/lib/types"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { CreateAccountDialog } from "@/components/accounts/create-account-dialog"
import { ContasTable, type ContaRow } from "@/components/contas/contas-table"

export const dynamic = "force-dynamic"

const PERIODS = [
  { value: "day", label: "Dia", ms: 24 * 60 * 60 * 1000 },
  { value: "week", label: "Semana", ms: 7 * 24 * 60 * 60 * 1000 },
  { value: "month", label: "Mês", ms: 30 * 24 * 60 * 60 * 1000 },
] as const

type Period = (typeof PERIODS)[number]["value"]

export default async function ContasPage({
  searchParams,
}: {
  searchParams: Promise<{ period?: string }>
}) {
  const { period: rawPeriod } = await searchParams
  const period: Period = PERIODS.some((p) => p.value === rawPeriod)
    ? (rawPeriod as Period)
    : "day"
  const periodDef = PERIODS.find((p) => p.value === period)!
  const periodStart = new Date(Date.now() - periodDef.ms).toISOString()

  const db = createSupabaseAdmin()

  const [
    { data: accountsData },
    { data: machinesData },
    { data: routingData },
    { data: lorasData },
    { data: keysData },
    { data: usageData },
  ] = await Promise.all([
    db.from("accounts").select("*").order("name"),
    db.from("machines").select("*").neq("status", "terminated"),
    db.from("routing_state").select("*"),
    db.from("lora_adapters").select("account_id, status"),
    db.from("api_keys").select("id, account_id"),
    db
      .from("usage_metrics")
      .select("api_key_id, tokens_in, tokens_out, window_start")
      .gte("window_start", periodStart),
  ])

  const accounts = (accountsData ?? []) as Account[]
  const machines = (machinesData ?? []) as Machine[]
  const routes = (routingData ?? []) as RoutingState[]
  const loras = (lorasData ?? []) as Pick<LoraAdapter, "account_id" | "status">[]
  const keys = (keysData ?? []) as Pick<ApiKey, "id" | "account_id">[]
  const usage = (usageData ?? []) as {
    api_key_id: string | null
    tokens_in: number
    tokens_out: number
    window_start: string
  }[]

  const machineById = new Map(machines.map((m) => [m.id, m]))
  const routeByAccount = new Map(routes.map((r) => [r.account_id, r]))
  const accountIdByKeyId = new Map(keys.map((k) => [k.id, k.account_id]))

  const tokensByAccount = new Map<string, number>()
  for (const u of usage) {
    const accountId = u.api_key_id ? accountIdByKeyId.get(u.api_key_id) : undefined
    if (!accountId) continue
    tokensByAccount.set(
      accountId,
      (tokensByAccount.get(accountId) ?? 0) + u.tokens_in + u.tokens_out
    )
  }

  const readyAdapterAccounts = new Set(
    loras.filter((l) => l.status === "ready").map((l) => l.account_id)
  )

  const runningMachines = machines.filter((m) => m.status === "running")
  const activeMachines = machines.filter((m) =>
    ["running", "stopped"].includes(m.status)
  )
  const allocatedAccounts = accounts.filter(
    (a) => routeByAccount.get(a.id)?.machine_id
  )

  const kpis = [
    { label: "Total de contas", value: accounts.length, icon: Users },
    { label: "Contas alocadas", value: allocatedAccounts.length, icon: KeyRound },
    { label: "Máquinas rodando", value: runningMachines.length, icon: Server },
    { label: "Máquinas (total)", value: activeMachines.length, icon: ServerCog },
  ]

  const rows: ContaRow[] = accounts.map((account) => {
    const route = routeByAccount.get(account.id)
    const currentMachine = route?.machine_id ? machineById.get(route.machine_id) : undefined
    return {
      account,
      route,
      currentMachine,
      plan: readyAdapterAccounts.has(account.id) ? "Avançado" : "Básico",
      tokens: tokensByAccount.get(account.id) ?? 0,
    }
  })

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Contas</h1>
          <p className="text-sm text-muted-foreground">
            Contas, alocação de máquina e consumo de tokens
          </p>
        </div>
        <CreateAccountDialog />
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
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Contas</CardTitle>
            <CardDescription>{accounts.length} conta(s)</CardDescription>
          </div>
          <div className="flex items-center gap-1 rounded-lg border p-1">
            {PERIODS.map((p) => (
              <Link
                key={p.value}
                href={`/contas?period=${p.value}`}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  p.value === period
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                }`}
              >
                {p.label}
              </Link>
            ))}
          </div>
        </CardHeader>
        <CardContent>
          <ContasTable rows={rows} runningMachines={runningMachines} periodLabel={periodDef.label} />
        </CardContent>
      </Card>
    </div>
  )
}
