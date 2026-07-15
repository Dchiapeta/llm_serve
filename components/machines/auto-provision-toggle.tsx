"use client"

import * as React from "react"
import { toast } from "sonner"

import { setAutoProvisionEnabled } from "@/lib/actions"
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

// Interruptor global do provisionamento automático de máquina (cascata
// reativa numa request + reposição proativa a cada 5min no gateway). Nasce
// desligado (migration 0016_system_settings.sql) — é uma automação que gasta
// GPU sozinha, por isso pede confirmação só pra LIGAR (desligar é sempre
// seguro, não precisa confirmar).
export function AutoProvisionToggle({ initialEnabled }: { initialEnabled: boolean }) {
  const [enabled, setEnabled] = React.useState(initialEnabled)
  const [confirmOpen, setConfirmOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function apply(next: boolean) {
    startTransition(async () => {
      const result = await setAutoProvisionEnabled(next)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      setEnabled(next)
      toast.success(
        next
          ? "Provisionamento automático ligado — reservas sendo criadas"
          : "Provisionamento automático desligado"
      )
    })
  }

  function onCheckedChange(next: boolean) {
    if (next) {
      setConfirmOpen(true)
      return
    }
    apply(false)
  }

  return (
    <Card>
      <CardContent className="flex items-center justify-between gap-4 py-4">
        <div className="flex flex-col gap-1">
          <Label htmlFor="auto-provision-toggle" className="text-sm font-medium">
            Provisionamento automático de máquinas
          </Label>
          <p className="text-sm text-muted-foreground">
            Cria e pausa máquinas de reserva sozinho quando a capacidade de um
            plano fica baixa. Desligado por padrão — liga só quando quiser
            que o sistema comece a criar máquinas automaticamente.
          </p>
        </div>
        <Switch
          id="auto-provision-toggle"
          checked={enabled}
          disabled={pending}
          onCheckedChange={onCheckedChange}
        />
      </CardContent>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Ligar o provisionamento automático?</AlertDialogTitle>
            <AlertDialogDescription>
              O gateway vai poder criar máquinas novas sozinho (custo real de
              GPU) sempre que um plano ficar com poucos slots livres ou sem
              reserva. Ao confirmar, ele já cria as reservas que estiverem
              faltando agora mesmo, sem esperar o próximo ciclo automático.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                apply(true)
                setConfirmOpen(false)
              }}
            >
              Ligar
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
