"use client"

import * as React from "react"
import { toast } from "sonner"

import { setOpenRouterEnabled } from "@/lib/actions"
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

// Interruptor do repasse ao OpenRouter (system_settings.openrouter_enabled,
// migration 0071). Confirma para LIGAR, como o AutoProvisionToggle: é o lado
// que passa a gastar crédito a cada requisição.
export function OpenRouterToggle({
  initialEnabled,
  activeModels,
}: {
  initialEnabled: boolean
  activeModels: number
}) {
  const [enabled, setEnabled] = React.useState(initialEnabled)
  const [confirmOpen, setConfirmOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function apply(next: boolean) {
    startTransition(async () => {
      const result = await setOpenRouterEnabled(next)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      setEnabled(next)
      toast.success(
        next ? "Repasse ao OpenRouter ligado" : "Repasse ao OpenRouter desligado"
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
          <Label htmlFor="openrouter-toggle" className="text-sm font-medium">
            Repasse ao OpenRouter
          </Label>
          <p className="text-sm text-muted-foreground">
            {enabled
              ? `Ligado: requisições com um dos ${activeModels} modelo(s) ativo(s) abaixo no campo "model" são atendidas pelo OpenRouter.`
              : "Desligado: nenhuma requisição vai para o OpenRouter, mesmo com modelos ativos na lista."}
          </p>
        </div>
        <Switch
          id="openrouter-toggle"
          checked={enabled}
          disabled={pending}
          onCheckedChange={onCheckedChange}
        />
      </CardContent>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Ligar o repasse ao OpenRouter?</AlertDialogTitle>
            <AlertDialogDescription>
              Cada requisição de um modelo ativo passa a ser cobrada por token do
              crédito da Stac no OpenRouter. O custo de cada uma fica registrado
              em Requisições. {activeModels === 0 && "Ainda não há nenhum modelo ativo na lista."}
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
