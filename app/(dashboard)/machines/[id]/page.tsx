import Link from "next/link"
import { notFound } from "next/navigation"
import { ExternalLink } from "lucide-react"

import { computeCapacity } from "@/lib/capacity"
import { reconcileMachineStatuses } from "@/lib/machines"
import { runpod } from "@/lib/runpod"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Account, ApiKey, Machine, Template, UsageMetric } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { CreateKeyDialog } from "@/components/accounts/create-key-dialog"
import { RevokeKeyButton } from "@/components/accounts/revoke-key-button"
import { CapacityBar } from "@/components/machines/capacity-bar"
import { MachineActions } from "@/components/machines/machine-actions"
import { MachineLogs } from "@/components/machines/machine-logs"
import { StatusBadge } from "@/components/machines/status-badge"

export const dynamic = "force-dynamic"

type KeyWithAccount = ApiKey & { accounts: { name: string } | null }

export default async function MachineDetailPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  const db = createSupabaseAdmin()

  const { data: machineData } = await db
    .from("machines")
    .select("*")
    .eq("id", id)
    .single<Machine>()
  if (!machineData) notFound()

  // Reflete o estado real do RunPod já no carregamento (não só via botão).
  const [machine] = await reconcileMachineStatuses([machineData], db)

  const [{ data: tplData }, { data: keysData }, { data: accountsData }, { data: usageData }] =
    await Promise.all([
      machine.template_id
        ? db.from("templates").select("*").eq("id", machine.template_id).single<Template>()
        : Promise.resolve({ data: null }),
      db
        .from("api_keys")
        .select("*, accounts(name)")
        .eq("machine_id", id)
        .order("created_at", { ascending: false }),
      db.from("accounts").select("*").order("name"),
      db
        .from("usage_metrics")
        .select("*")
        .eq("machine_id", id)
        .order("window_start", { ascending: false })
        .limit(500),
    ])

  const template = tplData as Template | null
  const keys = (keysData ?? []) as KeyWithAccount[]
  const activeKeys = keys.filter((k) => k.status === "active")
  const accounts = (accountsData ?? []) as Account[]
  const usage = (usageData ?? []) as UsageMetric[]

  // env vars reais do pod (se ainda existir no RunPod)
  let podEnv: Record<string, string> = template?.env ?? {}
  if (machine.runpod_pod_id && machine.status !== "terminated") {
    try {
      const pod = await runpod.getPod(machine.runpod_pod_id)
      if (pod.env) podEnv = pod.env
    } catch {
      // pod pode não existir mais; usa env do template como referência
    }
  }

  const cap = computeCapacity({
    vramGb: machine.vram_gb,
    modelFootprintGb: template?.model_footprint_gb ?? 16,
    kvReserveGbPerUser: template?.kv_reserve_gb_per_user ?? 2,
    activeKeys: activeKeys.length,
  })

  const usageByKey = new Map<string, { requests: number; tokensIn: number; tokensOut: number }>()
  for (const u of usage) {
    if (!u.api_key_id) continue
    const acc = usageByKey.get(u.api_key_id) ?? { requests: 0, tokensIn: 0, tokensOut: 0 }
    acc.requests += u.requests
    acc.tokensIn += u.tokens_in
    acc.tokensOut += u.tokens_out
    usageByKey.set(u.api_key_id, acc)
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold">{machine.name}</h1>
            <StatusBadge status={machine.status} />
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {machine.gpu_type} · {machine.model_name ?? "sem modelo"} ·{" "}
            {machine.cost_per_hr ? `$${machine.cost_per_hr.toFixed(2)}/h` : "custo desconhecido"}
          </p>
          {machine.public_url && (
            <Link
              href={machine.public_url}
              target="_blank"
              className="mt-1 inline-flex items-center gap-1 font-mono text-xs text-muted-foreground hover:underline"
            >
              {machine.public_url} <ExternalLink className="size-3" />
            </Link>
          )}
        </div>
        <MachineActions machine={machine} />
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardDescription>Alocação de capacidade</CardDescription>
            <CardTitle className="text-2xl">{cap.usagePct}%</CardTitle>
          </CardHeader>
          <CardContent>
            <CapacityBar used={cap.slotsUsed} max={cap.slotsMax} />
            <p className="mt-2 text-xs text-muted-foreground">
              {cap.slotsUsed} de {cap.slotsMax} slots · {cap.slotsFree} livres
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>VRAM</CardDescription>
            <CardTitle className="text-2xl">
              {machine.vram_gb ? `${machine.vram_gb} GB` : "—"}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              Modelo ocupa ~{template?.model_footprint_gb ?? "?"} GB ·{" "}
              {template?.kv_reserve_gb_per_user ?? "?"} GB reservados por usuário
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Requisições (janelas coletadas)</CardDescription>
            <CardTitle className="text-2xl">
              {usage.reduce((s, u) => s + u.requests, 0).toLocaleString("pt-BR")}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              {usage.reduce((s, u) => s + u.tokens_in + u.tokens_out, 0).toLocaleString("pt-BR")}{" "}
              tokens processados
            </p>
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="accounts">
        <TabsList>
          <TabsTrigger value="accounts">Contas & Slots</TabsTrigger>
          <TabsTrigger value="logs">Logs</TabsTrigger>
          <TabsTrigger value="env">Variáveis</TabsTrigger>
        </TabsList>

        <TabsContent value="accounts" className="mt-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <div>
                <CardTitle>Contas nesta máquina</CardTitle>
                <CardDescription>
                  {activeKeys.length} chave(s) ativa(s) · {cap.slotsFree} slot(s) livre(s)
                </CardDescription>
              </div>
              <CreateKeyDialog
                accounts={accounts}
                machines={[machine]}
                fixedMachineId={machine.id}
              />
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Conta</TableHead>
                    <TableHead>Chave</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Requisições</TableHead>
                    <TableHead>Tokens</TableHead>
                    <TableHead>Criada em</TableHead>
                    <TableHead className="w-24" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {keys.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={7} className="text-center text-muted-foreground">
                        Nenhuma chave criada para esta máquina.
                      </TableCell>
                    </TableRow>
                  )}
                  {keys.map((k) => {
                    const u = usageByKey.get(k.id)
                    return (
                      <TableRow key={k.id}>
                        <TableCell className="font-medium">
                          {k.accounts?.name ?? "—"}
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {k.key_prefix}…
                        </TableCell>
                        <TableCell>
                          {k.status === "active" ? (
                            <Badge variant="secondary">ativa</Badge>
                          ) : (
                            <Badge variant="outline">revogada</Badge>
                          )}
                        </TableCell>
                        <TableCell>{(u?.requests ?? 0).toLocaleString("pt-BR")}</TableCell>
                        <TableCell>
                          {((u?.tokensIn ?? 0) + (u?.tokensOut ?? 0)).toLocaleString("pt-BR")}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {new Date(k.created_at).toLocaleDateString("pt-BR")}
                        </TableCell>
                        <TableCell>
                          {k.status === "active" && (
                            <RevokeKeyButton keyId={k.id} keyPrefix={k.key_prefix} />
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="logs" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Logs</CardTitle>
              <CardDescription>
                Logs da máquina inteira ou filtrados por usuário (via agent)
              </CardDescription>
            </CardHeader>
            <CardContent>
              <MachineLogs
                machineId={machine.id}
                keys={activeKeys.map((k) => ({
                  key_prefix: k.key_prefix,
                  account_name: k.accounts?.name ?? "desconhecida",
                }))}
              />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="env" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Variáveis de ambiente</CardTitle>
              <CardDescription>
                Env vars do pod no RunPod (valores sensíveis: trate com cuidado)
              </CardDescription>
            </CardHeader>
            <CardContent>
              {Object.keys(podEnv).length === 0 ? (
                <p className="text-sm text-muted-foreground">Nenhuma variável.</p>
              ) : (
                <div className="flex flex-col">
                  {Object.entries(podEnv).map(([key, value], i) => (
                    <div key={key}>
                      {i > 0 && <Separator />}
                      <div className="grid grid-cols-[240px_1fr] gap-4 py-2">
                        <code className="font-mono text-xs font-semibold">{key}</code>
                        <code className="break-all font-mono text-xs text-muted-foreground">
                          {key === "AGENT_ADMIN_SECRET" ? "••••••••" : value}
                        </code>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
