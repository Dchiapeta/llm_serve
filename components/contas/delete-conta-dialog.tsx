"use client"

import * as React from "react"
import { toast } from "sonner"

import { deleteAccount } from "@/lib/actions"
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
import type { UsuarioRow } from "@/components/contas/usuarios-table"

export function DeleteContaDialog({
  conta,
  open,
  onOpenChange,
}: {
  conta: UsuarioRow
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [pending, startTransition] = React.useTransition()

  // Mesma guarda que deleteAccount aplica no servidor (lá ela é a que vale,
  // contra corrida). Aqui ela existe para o admin ver o impedimento e o que
  // fazer a respeito antes de clicar, em vez de levar um toast de erro.
  const blocking = conta.stackList.filter(
    (s) => s.machineStatus !== null && s.machineStatus !== "terminated"
  )

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {blocking.length > 0 ? "Não dá para apagar ainda" : "Apagar conta?"}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {blocking.length > 0 ? (
              <>
                A conta <strong>{conta.name}</strong> tem stack em máquina
                ativa. Desative ou migre antes de apagar — apagar conta nunca
                encerra máquina, porque um mesmo pod hospeda stacks de vários
                clientes.
              </>
            ) : (
              <>
                A conta <strong>{conta.name}</strong> e tudo que pende dela
                somem: stacks, chaves, base de conhecimento, adapters e o
                histórico de uso. O login continua existindo — se a pessoa
                entrar de novo pelo TryStac, uma conta nova e vazia é criada.
              </>
            )}
          </AlertDialogDescription>
        </AlertDialogHeader>

        {blocking.length > 0 && (
          <ul className="text-muted-foreground space-y-1 text-sm">
            {blocking.map((stack) => (
              <li key={stack.id}>
                <code className="font-mono text-xs">{stack.slug}</code> em{" "}
                {stack.machineName}
              </li>
            ))}
          </ul>
        )}

        <AlertDialogFooter>
          <AlertDialogCancel>
            {blocking.length > 0 ? "Fechar" : "Cancelar"}
          </AlertDialogCancel>
          {blocking.length === 0 && (
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                startTransition(async () => {
                  try {
                    const result = await deleteAccount(conta.id)
                    if (result?.error) {
                      toast.error(result.error)
                      return
                    }
                    toast.success("Conta apagada")
                    onOpenChange(false)
                  } catch (err) {
                    // requireAdminSession redireciona, e o Next sinaliza
                    // redirect lançando — re-lançar, senão vira toast e a
                    // navegação não acontece.
                    if (err && typeof err === "object" && "digest" in err) throw err
                    toast.error(
                      err instanceof Error ? err.message : "Erro ao apagar"
                    )
                  }
                })
              }}
            >
              Apagar
            </AlertDialogAction>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
