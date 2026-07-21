"use client"

import * as React from "react"
import { Pause, Play, RefreshCw, RotateCcw, Trash2 } from "lucide-react"
import { toast } from "sonner"

import {
  recreateMachine,
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
  const [recreateOpen, setRecreateOpen] = React.useState(false)
  const [stopForceOpen, setStopForceOpen] = React.useState(false)
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
          // Máquina em uso: oferece pausar mesmo assim (force)
          if (result.code === "in_use") setStopForceOpen(true)
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
          onClick={() => run(() => stopMachine(machine.id), "Máquina pausada")}
        >
          <Pause className="size-4" /> Pausar
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

      {(machine.status === "stopped" || machine.status === "error") && (
        <Button
          variant="outline"
          size="sm"
          disabled={pending}
          onClick={() => setRecreateOpen(true)}
        >
          <RotateCcw className="size-4" /> Recriar
        </Button>
      )}

      <AlertDialog open={stopForceOpen} onOpenChange={setStopForceOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Pausar mesmo com uso ativo?</AlertDialogTitle>
            <AlertDialogDescription>
              “{machine.name}” tem requisições em andamento. Pausar agora
              corta os streams em voo dessas contas. Prefira esperar ficar
              ociosa (a auto-pausa faz isso sem cortar ninguém).
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                run(() => stopMachine(machine.id, { force: true }), "Máquina pausada")
                setStopForceOpen(false)
              }}
            >
              Pausar mesmo assim
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

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
