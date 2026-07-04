"use client"

import * as React from "react"
import { Ban } from "lucide-react"
import { toast } from "sonner"

import { revokeKey } from "@/lib/actions"
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

export function RevokeKeyButton({
  keyId,
  keyPrefix,
}: {
  keyId: string
  keyPrefix: string
}) {
  const [pending, startTransition] = React.useTransition()

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="ghost" size="sm" className="text-destructive">
          <Ban className="size-4" /> Revogar
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Revogar chave {keyPrefix}…?</AlertDialogTitle>
          <AlertDialogDescription>
            O usuário perde o acesso imediatamente após o sync com a máquina.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancelar</AlertDialogCancel>
          <AlertDialogAction
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                try {
                  await revokeKey(keyId)
                  toast.success("Chave revogada")
                } catch (e) {
                  toast.error(e instanceof Error ? e.message : "Erro ao revogar")
                }
              })
            }
          >
            Revogar
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
