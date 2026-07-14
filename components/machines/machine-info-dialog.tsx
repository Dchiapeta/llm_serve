"use client"

import { ExternalLink, Info } from "lucide-react"

import type { MachineDisplayStatus } from "@/lib/machines"
import type { Machine } from "@/lib/types"
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog"
import { Progress } from "@/components/ui/progress"
import { StatusBadge } from "@/components/machines/status-badge"

export function MachineInfoDialog({
  machine,
  displayStatus,
  templateName,
  capacity,
  open,
  onOpenChange,
}: {
  machine: Machine
  displayStatus: MachineDisplayStatus
  templateName: string | undefined
  capacity: { usagePct: number; slotsUsed: number; slotsMax: number }
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xs">
        <DialogTitle className="sr-only">
          Informações da máquina {machine.name}
        </DialogTitle>
        <div className="text-muted-foreground -m-4 mb-0 flex items-center gap-2 border-b px-4 py-3 [&_svg]:size-4">
          <Info aria-hidden="true" />
          <span className="text-foreground text-sm font-medium">
            {machine.name}
          </span>
        </div>

        <div className="space-y-3">
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Status</dt>
            <dd>
              <StatusBadge status={displayStatus} />
            </dd>

            <dt className="text-muted-foreground">GPU</dt>
            <dd>{machine.gpu_type}</dd>

            <dt className="text-muted-foreground">Modelo</dt>
            <dd className="truncate font-mono text-xs" title={machine.model_name ?? undefined}>
              {machine.model_name ?? "—"}
            </dd>

            <dt className="text-muted-foreground">Produto</dt>
            <dd>{templateName ?? "—"}</dd>

            <dt className="text-muted-foreground">VRAM</dt>
            <dd>{machine.vram_gb ? `${machine.vram_gb} GB` : "—"}</dd>

            <dt className="text-muted-foreground">Custo/h</dt>
            <dd>
              {machine.cost_per_hr ? `$${machine.cost_per_hr.toFixed(2)}` : "—"}
            </dd>

            <dt className="text-muted-foreground">Criada em</dt>
            <dd>{new Date(machine.created_at).toLocaleString("pt-BR")}</dd>
          </dl>

          <div className="space-y-1.5">
            <div className="text-muted-foreground flex items-center justify-between text-xs">
              <span>Slots</span>
              <span>
                {capacity.slotsUsed}/{capacity.slotsMax}
              </span>
            </div>
            <Progress value={capacity.usagePct} />
          </div>

          {machine.public_url && (
            <a
              href={machine.public_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary inline-flex items-center gap-1 text-xs font-medium hover:underline"
            >
              <ExternalLink aria-hidden="true" className="size-2.5 shrink-0" />
              Abrir endpoint
            </a>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
