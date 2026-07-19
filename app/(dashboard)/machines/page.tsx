import Link from "next/link"

import { getAutoProvisionEnabled } from "@/lib/actions"
import { computeCapacity, stackWeight } from "@/lib/capacity"
import { machineDisplayStatus, reconcileMachineStatuses } from "@/lib/machines"
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
import { AutoProvisionToggle } from "@/components/machines/auto-provision-toggle"
import { CreateMachineDialog } from "@/components/machines/create-machine-dialog"
import { MachineRowActions } from "@/components/machines/machine-row-actions"
import { PlanBadge } from "@/components/machines/plan-badge"
import { StatusBadge } from "@/components/machines/status-badge"

export const dynamic = "force-dynamic"

export default async function MachinesPage() {
  const db = createSupabaseAdmin()

  const [{ data: machinesData }, { data: templatesData }, gpus, autoProvisionEnabled] =
    await Promise.all([
      db
        .from("machines")
        .select("*")
        .neq("status", "terminated")
        .order("created_at", { ascending: false }),
      db.from("templates").select("*").order("name"),
      listGpuTypes().catch(() => []),
      getAutoProvisionEnabled(),
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

  // "Rodando" só quando o vLLM já responde; antes disso, "Subindo".
  const displayStatuses = await Promise.all(machines.map(machineDisplayStatus))
  const displayStatusById = new Map(
    machines.map((m, i) => [m.id, displayStatuses[i]])
  )

  // Ocupação = stacks hospedadas PONDERADAS pela classe de uso (0032), não
  // chaves ativas: stacks da mesma conta compartilham uma chave e sumiriam
  // da contagem.
  const { data: stackRows } = await db
    .from("stacks")
    .select("machine_id, usage_class")
    .not("machine_id", "is", null)
  const stacksByMachine = new Map<string, number>()
  for (const s of stackRows ?? []) {
    stacksByMachine.set(
      s.machine_id,
      (stacksByMachine.get(s.machine_id) ?? 0) + stackWeight(s.usage_class)
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

      <AutoProvisionToggle initialEnabled={autoProvisionEnabled} />

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
                <TableHead>Plano</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>GPU</TableHead>
                <TableHead>Modelo</TableHead>
                <TableHead>Slots</TableHead>
                <TableHead>Custo/h</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {machines.length === 0 && (
                <TableRow>
                  <TableCell colSpan={8} className="text-center text-muted-foreground">
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
                  occupied: stacksByMachine.get(m.id) ?? 0,
                  maxUsers: m.max_users,
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
                      <PlanBadge plan={tpl?.plan} />
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={displayStatusById.get(m.id) ?? m.status} />
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
                    <TableCell>
                      <MachineRowActions
                        machine={m}
                        displayStatus={displayStatusById.get(m.id) ?? m.status}
                        templateName={tpl?.name}
                        capacity={cap}
                      />
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
