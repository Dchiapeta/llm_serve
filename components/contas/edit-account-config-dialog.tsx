"use client"

import * as React from "react"
import { toast } from "sonner"

import { updateAccountConfig } from "@/lib/actions"
import type { Account } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"

export function EditAccountConfigDialog({
  account,
  open,
  onOpenChange,
}: {
  account: Account
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [systemPrompt, setSystemPrompt] = React.useState(account.system_prompt ?? "")
  const [pending, startTransition] = React.useTransition()

  function onSave() {
    startTransition(async () => {
      try {
        const formData = new FormData()
        formData.set("account_id", account.id)
        formData.set("plan", account.plan)
        formData.set("system_prompt", systemPrompt)
        await updateAccountConfig(formData)
        toast.success("Configuração atualizada")
        onOpenChange(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao atualizar configuração")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Plano e system prompt — {account.name}</DialogTitle>
          <DialogDescription>
            O system prompt configurado aqui é injetado pelo gateway em toda
            chamada de chat completions desta conta.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="system-prompt">System prompt</Label>
            <Textarea
              id="system-prompt"
              placeholder="Ex: Você é um assistente de código especializado em..."
              rows={6}
              className="max-h-[60vh] resize-y overflow-y-auto font-mono text-xs"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
          </div>
          <Button onClick={onSave} disabled={pending}>
            {pending ? "Salvando…" : "Salvar"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
