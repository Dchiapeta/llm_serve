"use client"

import * as React from "react"
import { Play, RefreshCw, Square, Trash2 } from "lucide-react"
import { toast } from "sonner"

import {
  refreshMachineStatus,
  startMachine,
  stopMachine,
  terminateMachine,
} from "@/lib/actions"
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
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"

export function MachineActions({ machine }: { machine: Machine }) {
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
    <div className="flex items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        disabled={pending}
        onClick={() => run(() => refreshMachineStatus(machine.id), "Status atualizado")}
      >
        <RefreshCw className="size-4" /> Atualizar
      </Button>

      {machine.status === "running" ? (
        <Button
          variant="outline"
          size="sm"
          disabled={pending}
          onClick={() => run(() => stopMachine(machine.id), "Máquina desativada")}
        >
          <Square className="size-4" /> Desativar
        </Button>
      ) : machine.status === "stopped" ? (
        <Button
          variant="outline"
          size="sm"
          disabled={pending}
          onClick={() => run(() => startMachine(machine.id), "Máquina iniciada")}
        >
          <Play className="size-4" /> Iniciar
        </Button>
      ) : null}

      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button variant="destructive" size="sm" disabled={pending}>
            <Trash2 className="size-4" /> Apagar
          </Button>
        </AlertDialogTrigger>
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
              onClick={() => run(() => terminateMachine(machine.id), "Máquina apagada")}
            >
              Apagar definitivamente
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
