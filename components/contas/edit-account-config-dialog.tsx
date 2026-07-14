"use client"

import * as React from "react"
import { toast } from "sonner"

import { updateAccountConfig } from "@/lib/actions"
import { TEMPLATE_PLANS, type Account, type TemplatePlan } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
  const [plan, setPlan] = React.useState<TemplatePlan>(account.plan)
  const [systemPrompt, setSystemPrompt] = React.useState(account.system_prompt ?? "")
  const [pending, startTransition] = React.useTransition()

  function onSave() {
    startTransition(async () => {
      try {
        const formData = new FormData()
        formData.set("account_id", account.id)
        formData.set("plan", plan)
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
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Plano e system prompt — {account.name}</DialogTitle>
          <DialogDescription>
            O system prompt configurado aqui é injetado pelo gateway em toda
            chamada de chat completions desta conta.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label>Plano</Label>
            <Select value={plan} onValueChange={(v) => setPlan(v as TemplatePlan)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TEMPLATE_PLANS.map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="system-prompt">System prompt</Label>
            <Textarea
              id="system-prompt"
              placeholder="Ex: Você é um assistente de código especializado em..."
              rows={6}
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
