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
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select"

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
            <Label htmlFor="template_id">Template</Label>
            <NativeSelect
              id="template_id"
              name="template_id"
              required
              value={templateId}
              onChange={(e) => setTemplateId(e.target.value)}
              className="w-full"
            >
              <NativeSelectOption value="" disabled>
                Escolha um template
              </NativeSelectOption>
              {templates.map((t) => (
                <NativeSelectOption key={t.id} value={t.id}>
                  {t.name} — {t.model_name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="gpu_type">GPU</Label>
            <NativeSelect
              id="gpu_type"
              name="gpu_type"
              required
              disabled={!selectedTemplate}
              defaultValue=""
              className="w-full"
            >
              <NativeSelectOption value="" disabled>
                {selectedTemplate
                  ? "Escolha a GPU"
                  : "Escolha um template primeiro"}
              </NativeSelectOption>
              {availableGpus.map((g) => (
                <NativeSelectOption key={g.id} value={g.id}>
                  {g.displayName} — {g.memoryInGb} GB
                  {g.securePrice ? ` — $${g.securePrice.toFixed(2)}/h` : ""}
                </NativeSelectOption>
              ))}
            </NativeSelect>
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
