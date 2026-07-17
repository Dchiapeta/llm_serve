import { KeyRound, Server, ServerCog, Users } from "lucide-react"

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

const PERIOD_LABEL = "Dia"
const PERIOD_MS = 24 * 60 * 60 * 1000

export default async function ContasPage() {
  const periodStart = new Date(Date.now() - PERIOD_MS).toISOString()

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
    db
      .from("api_keys")
      .select("id, account_id, machine_id, stack_id, status, key_prefix, plain_key, created_at"),
    db
      .from("usage_metrics")
      .select("api_key_id, tokens_in, tokens_out, requests, window_start")
      .gte("window_start", periodStart),
    db.from("knowledge_chunks").select("stack_id, storage_path"),
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
    "id" | "account_id" | "machine_id" | "stack_id" | "status" | "key_prefix" | "plain_key" | "created_at"
  >[]
  const usage = (usageData ?? []) as {
    api_key_id: string | null
    tokens_in: number
    tokens_out: number
    requests: number
    window_start: string
  }[]
  const knowledgeChunks = (knowledgeData ?? []) as {
    stack_id: string | null
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

  // Ocupação = stacks hospedadas (1 stack = 1 slot), não chaves ativas:
  // stacks da mesma conta compartilham uma chave e sumiriam da contagem.
  const stacksCountByMachine = new Map<string, number>()
  for (const s of stacks) {
    if (!s.machine_id) continue
    stacksCountByMachine.set(s.machine_id, (stacksCountByMachine.get(s.machine_id) ?? 0) + 1)
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
      occupied: stacksCountByMachine.get(m.id) ?? 0,
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

  // Chunks sem stack_id são legados de contas que tinham 2+ stacks no
  // momento da migration 0020 (não deu para saber a qual pertenciam) — não
  // aparecem em nenhuma listagem específica até o admin re-indexar por stack.
  const knowledgeFilesByStack = new Map<string, Map<string, number>>()
  for (const chunk of knowledgeChunks) {
    if (!chunk.stack_id) continue
    const byPath = knowledgeFilesByStack.get(chunk.stack_id) ?? new Map()
    byPath.set(chunk.storage_path, (byPath.get(chunk.storage_path) ?? 0) + 1)
    knowledgeFilesByStack.set(chunk.stack_id, byPath)
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
    return (stacksByAccount.get(account.id) ?? []).map((s) => {
      const machine = s.machine_id ? machineById.get(s.machine_id) : undefined
      const knowledgeFiles = Array.from(
        knowledgeFilesByStack.get(s.id) ?? [],
        ([storage_path, chunks]) => ({ storage_path, chunks })
      )
      // Chaves pós-migration 0019 já sabem sua stack (stack_id); chaves
      // legadas (stack_id null) caem no heurístico antigo por conta+máquina,
      // que é ambíguo quando a conta tem múltiplas stacks na mesma máquina.
      const stackKeys = keys.filter((k) =>
        k.stack_id ? k.stack_id === s.id : k.account_id === account.id && k.machine_id === s.machine_id
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
        <CardHeader>
          <CardTitle>Stacks</CardTitle>
          <CardDescription>{rows.length} stack(s)</CardDescription>
        </CardHeader>
        <CardContent>
          <ContasTable
            rows={rows}
            runningMachines={runningMachines}
            periodLabel={PERIOD_LABEL}
            stackMachines={stackMachines}
            templates={templates}
          />
        </CardContent>
      </Card>
    </div>
  )
}
