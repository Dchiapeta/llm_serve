"use client"

import * as React from "react"
import { toast } from "sonner"

import { migrateStackToMachine } from "@/lib/actions"
import type { Machine } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export function MigrateAccountDialog({
  stackId,
  accountName,
  currentMachineName,
  eligibleMachines,
  open,
  onOpenChange,
}: {
  stackId: string
  accountName: string
  currentMachineName: string | undefined
  eligibleMachines: Machine[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [machineId, setMachineId] = React.useState("")
  const [pending, startTransition] = React.useTransition()

  function onMigrate() {
    startTransition(async () => {
      try {
        await migrateStackToMachine(stackId, machineId)
        toast.success("Stack migrada")
        onOpenChange(false)
        setMachineId("")
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao migrar conta")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Migrar {accountName}</DialogTitle>
          <DialogDescription>
            {currentMachineName
              ? `Move o adapter LoRA de "${currentMachineName}" para a máquina destino.`
              : "Carrega o adapter LoRA da conta na máquina destino."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <Label>Máquina destino</Label>
          <Select value={machineId} onValueChange={setMachineId}>
            <SelectTrigger>
              <SelectValue placeholder="Escolha a máquina" />
            </SelectTrigger>
            <SelectContent>
              {eligibleMachines.map((m) => (
                <SelectItem key={m.id} value={m.id}>
                  {m.name} — {m.model_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {eligibleMachines.length === 0 && (
            <p className="text-xs text-muted-foreground">
              Nenhuma outra máquina rodando disponível.
            </p>
          )}
        </div>

        <DialogFooter>
          <Button onClick={onMigrate} disabled={pending || !machineId}>
            {pending ? "Migrando…" : "Migrar"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
