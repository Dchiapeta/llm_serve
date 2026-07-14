"use client"

import * as React from "react"
import { toast } from "sonner"

import { deleteStack } from "@/lib/actions"
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

export function DeleteStackDialog({
  stackId,
  slug,
  open,
  onOpenChange,
}: {
  stackId: string
  slug: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [pending, startTransition] = React.useTransition()

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Apagar stack?</AlertDialogTitle>
            <AlertDialogDescription>
              A stack <code className="font-mono text-xs">{slug}</code> será
              removida do painel. A máquina que a hospeda não é afetada.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                startTransition(async () => {
                  try {
                    await deleteStack(stackId)
                    toast.success("Stack apagada")
                    onOpenChange(false)
                  } catch (err) {
                    toast.error(
                      err instanceof Error ? err.message : "Erro ao apagar"
                    )
                  }
                })
              }}
            >
              Apagar
            </AlertDialogAction>
          </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
