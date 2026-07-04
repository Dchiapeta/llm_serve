"use client"

import * as React from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"

import { createAccount } from "@/lib/actions"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export function CreateAccountDialog() {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await createAccount(formData)
        toast.success("Conta criada")
        setOpen(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao criar conta")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Nova conta
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Nova conta</DialogTitle>
          <DialogDescription>
            Uma conta representa um usuário que consome a LLM.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="name">Nome</Label>
            <Input id="name" name="name" required />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="email">E-mail (opcional)</Label>
            <Input id="email" name="email" type="email" />
          </div>
          <Button type="submit" disabled={pending}>
            {pending ? "Criando…" : "Criar conta"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
