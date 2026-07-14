"use client"

import * as React from "react"
import { Info, MoreVertical, Pause, Play, RotateCcw, Trash2 } from "lucide-react"
import { toast } from "sonner"

import {
  recreateMachine,
  startMachine,
  stopMachine,
  terminateMachine,
} from "@/lib/actions"
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
  const [recreateOpen, setRecreateOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function run(
    fn: () => Promise<{ error: string; code?: string } | void>,
    success: string
  ) {
    startTransition(async () => {
      try {
        const result = await fn()
        if (result?.error) {
          toast.error(result.error)
          // Host sem GPU livre: oferece recriar o pod em outro host
          if (result.code === "no_gpu_on_host") setRecreateOpen(true)
          return
        }
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
          {(machine.status === "stopped" || machine.status === "error") && (
            <DropdownMenuItem onSelect={() => setRecreateOpen(true)}>
              <RotateCcw className="size-4" />
              Recriar
            </DropdownMenuItem>
          )}
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

      <AlertDialog open={recreateOpen} onOpenChange={setRecreateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Recriar máquina em outro host?</AlertDialogTitle>
            <AlertDialogDescription>
              O pod atual de “{machine.name}” será terminado e um novo será
              criado com o mesmo template e GPU. O disco do container é perdido
              e o modelo baixa de novo no boot; as chaves de API são reenviadas
              automaticamente quando a máquina ficar pronta.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                run(
                  () => recreateMachine(machine.id),
                  "Máquina recriada — novo pod subindo"
                )
                setRecreateOpen(false)
              }}
            >
              Recriar máquina
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

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
