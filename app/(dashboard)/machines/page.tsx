import Link from "next/link"

import { computeCapacity } from "@/lib/capacity"
import { reconcileMachineStatuses } from "@/lib/machines"
import { listGpuTypes } from "@/lib/runpod"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Machine, Template } from "@/lib/types"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { CreateMachineDialog } from "@/components/machines/create-machine-dialog"
import { StatusBadge } from "@/components/machines/status-badge"

export const dynamic = "force-dynamic"

export default async function MachinesPage() {
  const db = createSupabaseAdmin()

  const [{ data: machinesData }, { data: templatesData }, gpus] =
    await Promise.all([
      db
        .from("machines")
        .select("*")
        .neq("status", "terminated")
        .order("created_at", { ascending: false }),
      db.from("templates").select("*").order("name"),
      listGpuTypes().catch(() => []),
    ])

  const templates = (templatesData ?? []) as Template[]

  // Reconcilia o status do banco com a realidade do RunPod e descarta as que
  // foram terminadas por fora (mesmo filtro aplicado na query).
  const reconciled = await reconcileMachineStatuses(
    (machinesData ?? []) as Machine[],
    db
  )
  const machines = reconciled.filter((m) => m.status !== "terminated")
  const templateById = new Map(templates.map((t) => [t.id, t]))

  const { data: keyCounts } = await db
    .from("api_keys")
    .select("machine_id")
    .eq("status", "active")
  const activeKeysByMachine = new Map<string, number>()
  for (const k of keyCounts ?? []) {
    activeKeysByMachine.set(
      k.machine_id,
      (activeKeysByMachine.get(k.machine_id) ?? 0) + 1
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Máquinas</h1>
          <p className="text-sm text-muted-foreground">
            Pods rodando LLMs no RunPod
          </p>
        </div>
        <CreateMachineDialog templates={templates} gpus={gpus} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Todas as máquinas</CardTitle>
          <CardDescription>{machines.length} máquina(s) ativa(s)</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Nome</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>GPU</TableHead>
                <TableHead>Modelo</TableHead>
                <TableHead>Slots</TableHead>
                <TableHead>Custo/h</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {machines.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    Nenhuma máquina ativa.
                  </TableCell>
                </TableRow>
              )}
              {machines.map((m) => {
                const tpl = m.template_id ? templateById.get(m.template_id) : undefined
                const cap = computeCapacity({
                  vramGb: m.vram_gb,
                  modelFootprintGb: tpl?.model_footprint_gb ?? 16,
                  kvReserveGbPerUser: tpl?.kv_reserve_gb_per_user ?? 2,
                  activeKeys: activeKeysByMachine.get(m.id) ?? 0,
                })
                return (
                  <TableRow key={m.id}>
                    <TableCell>
                      <Link
                        href={`/machines/${m.id}`}
                        className="font-medium hover:underline"
                      >
                        {m.name}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={m.status} />
                    </TableCell>
                    <TableCell className="text-sm">{m.gpu_type}</TableCell>
                    <TableCell className="font-mono text-xs">{m.model_name}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Progress value={cap.usagePct} className="w-24" />
                        <span className="text-xs text-muted-foreground">
                          {cap.slotsUsed}/{cap.slotsMax}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="text-sm">
                      {m.cost_per_hr ? `$${m.cost_per_hr.toFixed(2)}` : "—"}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
