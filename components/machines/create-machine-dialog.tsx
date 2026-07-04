"use client"

import * as React from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"

import { createMachine } from "@/lib/actions"
import type { GpuType } from "@/lib/runpod"
import type { Template } from "@/lib/types"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export function CreateMachineDialog({
  templates,
  gpus,
}: {
  templates: Template[]
  gpus: GpuType[]
}) {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()
  const [templateId, setTemplateId] = React.useState("")

  const selectedTemplate = templates.find((t) => t.id === templateId)
  // se o template lista GPUs compatíveis, restringe a seleção a elas
  const availableGpus =
    selectedTemplate && selectedTemplate.gpu_types.length > 0
      ? gpus.filter((g) => selectedTemplate.gpu_types.includes(g.id))
      : gpus

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await createMachine(formData)
        // redirect acontece na action; toast só em caso de erro
      } catch (e) {
        // redirect() lança um erro interno do Next — não tratar como falha
        if (e && typeof e === "object" && "digest" in e) throw e
        toast.error(e instanceof Error ? e.message : "Erro ao criar máquina")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Nova máquina
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Nova máquina</DialogTitle>
          <DialogDescription>
            Cria um pod no RunPod a partir de um template.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="name">Nome</Label>
            <Input id="name" name="name" placeholder="llm-prod-01" required />
          </div>
          <div className="flex flex-col gap-2">
            <Label>Template</Label>
            <Select
              name="template_id"
              required
              value={templateId}
              onValueChange={setTemplateId}
            >
              <SelectTrigger>
                <SelectValue placeholder="Escolha um template" />
              </SelectTrigger>
              <SelectContent>
                {templates.map((t) => (
                  <SelectItem key={t.id} value={t.id}>
                    {t.name} — {t.model_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label>GPU</Label>
            <Select name="gpu_type" required disabled={!selectedTemplate}>
              <SelectTrigger>
                <SelectValue
                  placeholder={
                    selectedTemplate
                      ? "Escolha a GPU"
                      : "Escolha um template primeiro"
                  }
                />
              </SelectTrigger>
              <SelectContent>
                {availableGpus.map((g) => (
                  <SelectItem key={g.id} value={g.id}>
                    {g.displayName} — {g.memoryInGb} GB
                    {g.securePrice ? ` — $${g.securePrice.toFixed(2)}/h` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selectedTemplate && availableGpus.length === 0 && (
              <p className="text-xs text-muted-foreground">
                Este template não tem GPUs compatíveis cadastradas.
              </p>
            )}
          </div>
          <Button type="submit" disabled={pending || templates.length === 0}>
            {pending ? "Criando pod…" : "Criar máquina"}
          </Button>
          {templates.length === 0 && (
            <p className="text-xs text-muted-foreground">
              Crie um template primeiro.
            </p>
          )}
        </form>
      </DialogContent>
    </Dialog>
  )
}
