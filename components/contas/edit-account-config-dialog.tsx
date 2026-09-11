"use client"

import * as React from "react"
import { TriangleAlert } from "lucide-react"
import { toast } from "sonner"

import { updateStackGenerationConfig } from "@/lib/generation-config"
import type { Stack } from "@/lib/types"
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
import { NativeSelect } from "@/components/ui/native-select"

export function EditAccountConfigDialog({
  stack,
  open,
  onOpenChange,
}: {
  stack: Pick<Stack, "id" | "slug" | "system_prompt" | "default_enable_thinking">
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [systemPrompt, setSystemPrompt] = React.useState(stack.system_prompt ?? "")
  const [thinking, setThinking] = React.useState(
    stack.default_enable_thinking == null ? "inherit" : stack.default_enable_thinking ? "on" : "off"
  )
  const [pending, startTransition] = React.useTransition()

  function onSave() {
    startTransition(async () => {
      try {
        const formData = new FormData()
        formData.set("stack_id", stack.id)
        formData.set("system_prompt", systemPrompt)
        formData.set("thinking", thinking)
        await updateStackGenerationConfig(formData)
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
          <DialogTitle>Comportamento — {stack.slug}</DialogTitle>
          <DialogDescription>
            Configure as instruções e o raciocínio da stack. Instruções de sistema
            enviadas pelo cliente têm prioridade sobre o prompt abaixo.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="thinking-mode">Raciocínio</Label>
            <NativeSelect id="thinking-mode" value={thinking} onChange={(e) => setThinking(e.target.value)}>
              <option value="inherit">Herdar comportamento atual</option>
              <option value="off">Desligado — respostas diretas</option>
              <option value="on">Ligado — tarefas complexas</option>
            </NativeSelect>
            <p className="text-muted-foreground text-xs">
              Disponível em modelos com alternância de raciocínio. Uma escolha
              explícita na requisição ou na chave tem prioridade. Ligado pode
              aumentar o tempo e os tokens de geração.
            </p>
          </div>
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
            <p className="text-muted-foreground flex items-start gap-1.5 text-xs">
              <TriangleAlert aria-hidden="true" className="mt-0.5 size-3 shrink-0" />
              Nunca coloque segredos aqui (chaves, URLs internas, dados
              pessoais). O texto é injetado literalmente em cada chamada e um
              usuário pode conseguir extraí-lo via prompt injection — não há
              garantia de que fique oculto.
            </p>
          </div>
          <Button onClick={onSave} disabled={pending}>
            {pending ? "Salvando…" : "Salvar"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
