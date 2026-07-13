import Link from "next/link"
import { notFound } from "next/navigation"
import { ExternalLink } from "lucide-react"

import { computeCapacity, computeLoraCapacity } from "@/lib/capacity"
import { machineDisplayStatus, reconcileMachineStatuses } from "@/lib/machines"
import { collectUsageMetrics } from "@/lib/metrics"
import { runpod, runpodConsoleUrl } from "@/lib/runpod"
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
import { MachineAbout } from "@/components/machines/machine-about"
import { MachineActions } from "@/components/machines/machine-actions"
import { StatusBadge } from "@/components/machines/status-badge"

export const dynamic = "force-dynamic"

type KeyWithAccount = ApiKey & { accounts: { name: string } | null }

// "3d 4h", "2h 15min", "38min"
function formatRuntime(ms: number): string {
  const totalMin = Math.max(0, Math.floor(ms / 60_000))
  const days = Math.floor(totalMin / (60 * 24))
  const hours = Math.floor((totalMin % (60 * 24)) / 60)
  const minutes = totalMin % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}min`
  return `${minutes}min`
}

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
  const displayStatus = await machineDisplayStatus(machine)

  // Puxa os contadores do agent para o banco antes de ler o uso.
  await collectUsageMetrics([machine], db)

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
  let lastStartedAt: string | null = null
  if (machine.runpod_pod_id && machine.status !== "terminated") {
    try {
      const pod = await runpod.getPod(machine.runpod_pod_id)
      if (pod.env) podEnv = pod.env
      lastStartedAt = pod.lastStartedAt ?? null
    } catch {
      // pod pode não existir mais; usa env do template como referência
    }
  }
  const runtime =
    machine.status === "running" && lastStartedAt
      ? formatRuntime(Date.now() - new Date(lastStartedAt).getTime())
      : null

  // Pod multi-LoRA: os slots passam a ser "quantos adapters cabem em VRAM
  // com o modelo base carregado", ocupados pelas rotas ativas (routing_state).
  const loraMode = podEnv.ENABLE_LORA === "true"
  let cap
  if (loraMode) {
    const { count: activeAdapters } = await db
      .from("routing_state")
      .select("account_id", { count: "exact", head: true })
      .eq("machine_id", id)
      .in("lora_status", ["loading", "loaded", "migrating"])
    cap = computeLoraCapacity({
      vramGb: machine.vram_gb,
      baseModelFootprintGb: template?.model_footprint_gb ?? 16,
      loraFootprintGb: template?.lora_footprint_gb ?? 0.5,
      kvReserveGbPerUser: template?.kv_reserve_gb_per_user ?? 2,
      activeAdapters: activeAdapters ?? 0,
      maxLoras: podEnv.MAX_LORAS ? Number(podEnv.MAX_LORAS) : 8,
    })
  } else {
    cap = computeCapacity({
      vramGb: machine.vram_gb,
      modelFootprintGb: template?.model_footprint_gb ?? 16,
      kvReserveGbPerUser: template?.kv_reserve_gb_per_user ?? 2,
      activeKeys: activeKeys.length,
      maxUsers: machine.max_users,
    })
  }

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
            <StatusBadge status={displayStatus} />
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
          {machine.runpod_pod_id && (
            <Link
              href={runpodConsoleUrl(machine.runpod_pod_id)}
              target="_blank"
              className="mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:underline"
            >
              Abrir no RunPod <ExternalLink className="size-3" />
            </Link>
          )}
        </div>
        <MachineActions machine={machine} />
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader>
            <CardDescription>Runtime</CardDescription>
            <CardTitle className="text-2xl">{runtime ?? "—"}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              {machine.status === "running" && lastStartedAt
                ? `No ar desde ${new Date(lastStartedAt).toLocaleString("pt-BR")}`
                : "Máquina não está rodando"}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>
              {loraMode ? "Alocação de adapters LoRA" : "Alocação de capacidade"}
            </CardDescription>
            <CardTitle className="text-2xl">{cap.usagePct}%</CardTitle>
          </CardHeader>
          <CardContent>
            <CapacityBar used={cap.slotsUsed} max={cap.slotsMax} />
            <p className="mt-2 text-xs text-muted-foreground">
              {cap.slotsUsed} de {cap.slotsMax}{" "}
              {loraMode ? "adapters em VRAM" : "slots"} · {cap.slotsFree} livres
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
          <TabsTrigger value="env">Variáveis</TabsTrigger>
          <TabsTrigger value="about">Sobre</TabsTrigger>
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

        <TabsContent value="about" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle>Como usar</CardTitle>
              <CardDescription>
                Exemplos de requisição para esta máquina via terminal e Claude Code CLI
              </CardDescription>
            </CardHeader>
            <CardContent>
              <MachineAbout
                publicUrl={machine.public_url}
                modelName={machine.model_name}
              />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
