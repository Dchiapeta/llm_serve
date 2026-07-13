"use client"

import * as React from "react"
import { Info, MoreVertical, Pause, Play, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { startMachine, stopMachine, terminateMachine } from "@/lib/actions"
import type { MachineDisplayStatus } from "@/lib/machines"
import type { Machine } from "@/lib/types"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { MachineInfoDialog } from "@/components/machines/machine-info-dialog"

export function MachineRowActions({
  machine,
  displayStatus,
  templateName,
  capacity,
}: {
  machine: Machine
  displayStatus: MachineDisplayStatus
  templateName: string | undefined
  capacity: { usagePct: number; slotsUsed: number; slotsMax: number }
}) {
  const [infoOpen, setInfoOpen] = React.useState(false)
  const [deleteOpen, setDeleteOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function run(fn: () => Promise<void>, success: string) {
    startTransition(async () => {
      try {
        await fn()
        toast.success(success)
      } catch (e) {
        if (e && typeof e === "object" && "digest" in e) throw e
        toast.error(e instanceof Error ? e.message : "Operação falhou")
      }
    })
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Ações para ${machine.name}`}
            disabled={pending}
          >
            <MoreVertical className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => setInfoOpen(true)}>
            <Info className="size-4" />
            Info
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {machine.status === "running" ? (
            <DropdownMenuItem
              onSelect={() => run(() => stopMachine(machine.id), "Máquina desativada")}
            >
              <Pause className="size-4" />
              Pausar
            </DropdownMenuItem>
          ) : machine.status === "stopped" ? (
            <DropdownMenuItem
              onSelect={() => run(() => startMachine(machine.id), "Máquina iniciada")}
            >
              <Play className="size-4" />
              Iniciar
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => setDeleteOpen(true)}
          >
            <Trash2 className="size-4" />
            Deletar
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <MachineInfoDialog
        machine={machine}
        displayStatus={displayStatus}
        templateName={templateName}
        capacity={capacity}
        open={infoOpen}
        onOpenChange={setInfoOpen}
      />

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Apagar máquina?</AlertDialogTitle>
            <AlertDialogDescription>
              O pod “{machine.name}” será terminado no RunPod. Essa ação não pode
              ser desfeita e o disco do container é perdido.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                run(() => terminateMachine(machine.id), "Máquina apagada")
                setDeleteOpen(false)
              }}
            >
              Apagar definitivamente
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
