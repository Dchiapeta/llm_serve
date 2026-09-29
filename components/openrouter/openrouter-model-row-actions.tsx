"use client"

import * as React from "react"
import { MoreHorizontal, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { deleteOpenRouterModel, setOpenRouterModelEnabled } from "@/lib/actions"
import type { OpenRouterModel } from "@/lib/types"
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
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Switch } from "@/components/ui/switch"

// Liga/desliga um modelo da allowlist sem apagá-lo: desligado, o gateway para
// de aceitá-lo em até alguns segundos (o painel pede o flush do cache).
export function OpenRouterModelEnabledSwitch({ model }: { model: OpenRouterModel }) {
  const [enabled, setEnabled] = React.useState(model.enabled)
  const [pending, startTransition] = React.useTransition()

  return (
    <Switch
      size="sm"
      checked={enabled}
      disabled={pending}
      aria-label={`Ativar ${model.slug}`}
      onCheckedChange={(next) =>
        startTransition(async () => {
          const result = await setOpenRouterModelEnabled(model.id, next)
          if (result?.error) {
            toast.error(result.error)
            return
          }
          setEnabled(next)
        })
      }
    />
  )
}

export function OpenRouterModelRowActions({ model }: { model: OpenRouterModel }) {
  const [deleteOpen, setDeleteOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label={`Ações para ${model.slug}`}>
            <MoreHorizontal className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem variant="destructive" onSelect={() => setDeleteOpen(true)}>
            <Trash2 className="size-4" />
            Remover
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remover {model.slug}?</AlertDialogTitle>
            <AlertDialogDescription>
              Clientes que usam este modelo passam a receber erro. O histórico de
              requisições dele continua em Requisições.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                startTransition(async () => {
                  const result = await deleteOpenRouterModel(model.id)
                  if (result?.error) {
                    toast.error(result.error)
                    return
                  }
                  toast.success("Modelo removido")
                  setDeleteOpen(false)
                })
              }}
            >
              Remover
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
