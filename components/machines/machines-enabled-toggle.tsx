"use client"

import * as React from "react"
import { toast } from "sonner"

import { setMachinesEnabled } from "@/lib/actions"
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
import { Card, CardContent } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"

// Interruptor das máquinas próprias (system_settings.machines_enabled,
// migration 0071). Ao contrário do AutoProvisionToggle, a confirmação é para
// DESLIGAR: é o lado que tira o serviço de quem usa modelo fora da lista do
// OpenRouter. Ligar só devolve o comportamento de sempre.
export function MachinesEnabledToggle({ initialEnabled }: { initialEnabled: boolean }) {
  const [enabled, setEnabled] = React.useState(initialEnabled)
  const [confirmOpen, setConfirmOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function apply(next: boolean) {
    startTransition(async () => {
      const result = await setMachinesEnabled(next)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      setEnabled(next)
      toast.success(
        next
          ? "Máquinas ligadas — requisições voltam a ir para as máquinas"
          : "Máquinas desligadas — nenhuma requisição vai para elas"
      )
    })
  }

  function onCheckedChange(next: boolean) {
    if (!next) {
      setConfirmOpen(true)
      return
    }
    apply(true)
  }

  return (
    <Card>
      <CardContent className="flex items-center justify-between gap-4 py-4">
        <div className="flex flex-col gap-1">
          <Label htmlFor="machines-enabled-toggle" className="text-sm font-medium">
            Máquinas próprias (RunPod)
          </Label>
          <p className="text-sm text-muted-foreground">
            {enabled
              ? "Ligado: requisições de modelos fora da lista do OpenRouter vão para as máquinas, que sobem, religam e são recriadas sozinhas quando preciso."
              : "Desligado: nenhuma requisição vai para as máquinas e nada é criado, religado ou recriado automaticamente. Só os modelos da página Modelos (OpenRouter) respondem."}
          </p>
        </div>
        <Switch
          id="machines-enabled-toggle"
          checked={enabled}
          disabled={pending}
          onCheckedChange={onCheckedChange}
        />
      </CardContent>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Desligar as máquinas?</AlertDialogTitle>
            <AlertDialogDescription>
              Requisições de modelos que não estão na lista do OpenRouter passam
              a falhar na hora, com um erro listando os modelos aceitos. As
              máquinas que estão ligadas agora não são paradas por este botão:
              sem tráfego, elas pausam sozinhas por ociosidade.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                apply(false)
                setConfirmOpen(false)
              }}
            >
              Desligar
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
