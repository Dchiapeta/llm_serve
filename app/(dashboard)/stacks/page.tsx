import { KeyRound, Server, ServerCog, Users } from "lucide-react"
import Link from "next/link"

import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Account, ApiKey, LoraAdapter, Machine, RoutingState, Stack, Template } from "@/lib/types"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { CreateStackDialog } from "@/components/contas/create-stack-dialog"
import { ContasTable, type StackRow } from "@/components/contas/contas-table"

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
    { data: knowledgeData },
    { data: stacksData },
    { data: templatesData },
  ] = await Promise.all([
    db.from("accounts").select("*").order("name"),
    db.from("machines").select("*").neq("status", "terminated"),
    db.from("routing_state").select("*"),
    db.from("lora_adapters").select("account_id, status"),
    db.from("api_keys").select("id, account_id, machine_id, status, key_prefix, plain_key, created_at"),
    db
      .from("usage_metrics")
      .select("api_key_id, tokens_in, tokens_out, requests, window_start")
      .gte("window_start", periodStart),
    db.from("knowledge_chunks").select("account_id, storage_path"),
    db.from("stacks").select("*").order("created_at"),
    db
      .from("templates")
      .select("id, name, plan, model_name, model_footprint_gb, kv_reserve_gb_per_user, gpu_types")
      .order("name"),
  ])

  const accounts = (accountsData ?? []) as Account[]
  const machines = (machinesData ?? []) as Machine[]
  const routes = (routingData ?? []) as RoutingState[]
  const loras = (lorasData ?? []) as Pick<LoraAdapter, "account_id" | "status">[]
  const keys = (keysData ?? []) as Pick<
    ApiKey,
    "id" | "account_id" | "machine_id" | "status" | "key_prefix" | "plain_key" | "created_at"
  >[]
  const usage = (usageData ?? []) as {
    api_key_id: string | null
    tokens_in: number
    tokens_out: number
    requests: number
    window_start: string
  }[]
  const knowledgeChunks = (knowledgeData ?? []) as {
    account_id: string
    storage_path: string
  }[]

  const stacks = (stacksData ?? []) as Stack[]
  const templates = (templatesData ?? []) as Pick<
    Template,
    "id" | "name" | "plan" | "model_name" | "model_footprint_gb" | "kv_reserve_gb_per_user" | "gpu_types"
  >[]

  const stacksByAccount = new Map<string, Stack[]>()
  for (const stack of stacks) {
    const list = stacksByAccount.get(stack.account_id) ?? []
    list.push(stack)
    stacksByAccount.set(stack.account_id, list)
  }

  const machineById = new Map(machines.map((m) => [m.id, m]))

  const activeKeysByMachine = new Map<string, number>()
  for (const k of keys) {
    if (k.status !== "active") continue
    activeKeysByMachine.set(k.machine_id, (activeKeysByMachine.get(k.machine_id) ?? 0) + 1)
  }

  // Máquinas candidatas a hospedar uma stack nova — sem admin_secret (client).
  const stackMachines = machines
    .filter((m) => m.status === "running" && m.template_id)
    .map((m) => ({
      id: m.id,
      name: m.name,
      template_id: m.template_id as string,
      model_name: m.model_name,
      vram_gb: m.vram_gb,
      max_users: m.max_users,
      activeKeys: activeKeysByMachine.get(m.id) ?? 0,
    }))
  const routeByAccount = new Map(routes.map((r) => [r.account_id, r]))

  const usageByKeyId = new Map<
    string,
    { tokensIn: number; tokensOut: number; requests: number }
  >()
  for (const u of usage) {
    if (!u.api_key_id) continue
    const agg = usageByKeyId.get(u.api_key_id) ?? {
      tokensIn: 0,
      tokensOut: 0,
      requests: 0,
    }
    agg.tokensIn += u.tokens_in
    agg.tokensOut += u.tokens_out
    agg.requests += u.requests
    usageByKeyId.set(u.api_key_id, agg)
  }

  const templateById = new Map(templates.map((t) => [t.id, t]))

  const readyAdapterAccounts = new Set(
    loras.filter((l) => l.status === "ready").map((l) => l.account_id)
  )

  const knowledgeFilesByAccount = new Map<string, Map<string, number>>()
  for (const chunk of knowledgeChunks) {
    const byPath = knowledgeFilesByAccount.get(chunk.account_id) ?? new Map()
    byPath.set(chunk.storage_path, (byPath.get(chunk.storage_path) ?? 0) + 1)
    knowledgeFilesByAccount.set(chunk.account_id, byPath)
  }

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

  // Uma linha por stack; contas sem stack não aparecem na tabela (os KPIs
  // continuam contando todas, e criar stack pra elas segue possível pelo
  // botão do header).
  const rows: StackRow[] = accounts.flatMap((account) => {
    const route = routeByAccount.get(account.id)
    const currentMachine = route?.machine_id ? machineById.get(route.machine_id) : undefined
    const hasReadyAdapter = readyAdapterAccounts.has(account.id)
    const knowledgeFiles = Array.from(
      knowledgeFilesByAccount.get(account.id) ?? [],
      ([storage_path, chunks]) => ({ storage_path, chunks })
    )
    return (stacksByAccount.get(account.id) ?? []).map((s) => {
      const machine = s.machine_id ? machineById.get(s.machine_id) : undefined
      const stackKeys = keys.filter(
        (k) => k.account_id === account.id && k.machine_id === s.machine_id
      )
      const stackUsage = { tokensIn: 0, tokensOut: 0, requests: 0 }
      for (const k of stackKeys) {
        const agg = usageByKeyId.get(k.id)
        if (!agg) continue
        stackUsage.tokensIn += agg.tokensIn
        stackUsage.tokensOut += agg.tokensOut
        stackUsage.requests += agg.requests
      }
      return {
        stack: {
          ...s,
          machineName: machine?.name,
          // Pick explícito — nunca a Machine inteira, para não vazar
          // admin_secret ao client.
          machine: machine && {
            id: machine.id,
            name: machine.name,
            gpu_type: machine.gpu_type,
            status: machine.status,
            model_name: machine.model_name,
            vram_gb: machine.vram_gb,
            cost_per_hr: machine.cost_per_hr,
            public_url: machine.public_url,
            max_users: machine.max_users,
            template_id: machine.template_id,
          },
          templateName: machine?.template_id
            ? templateById.get(machine.template_id)?.name
            : undefined,
          keys: stackKeys.map((k) => ({
            key_prefix: k.key_prefix,
            plain_key: k.plain_key,
            status: k.status,
            created_at: k.created_at,
          })),
          usage: stackUsage,
        },
        account,
        route,
        currentMachine,
        hasReadyAdapter,
        knowledgeFiles,
      }
    })
  })

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Stacks</h1>
          <p className="text-sm text-muted-foreground">
            Stacks contratadas, alocação de máquina e consumo de tokens
          </p>
        </div>
        <CreateStackDialog
          accounts={accounts}
          templates={templates}
          machines={stackMachines}
        />
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
            <CardTitle>Stacks</CardTitle>
            <CardDescription>{rows.length} stack(s)</CardDescription>
          </div>
          <div className="flex items-center gap-1 rounded-lg border p-1">
            {PERIODS.map((p) => (
              <Link
                key={p.value}
                href={`/stacks?period=${p.value}`}
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
          <ContasTable
            rows={rows}
            runningMachines={runningMachines}
            periodLabel={periodDef.label}
            stackMachines={stackMachines}
            templates={templates}
          />
        </CardContent>
      </Card>
    </div>
  )
}
