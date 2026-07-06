"use client"

import * as React from "react"
import { AlertCircle, Plus } from "lucide-react"

import { createMachine } from "@/lib/actions"
import { vramSlots } from "@/lib/capacity"
import type { GpuType } from "@/lib/runpod"
import type { Template } from "@/lib/types"
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
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
  const [gpuTypeId, setGpuTypeId] = React.useState("")
  const [maxUsers, setMaxUsers] = React.useState("")
  const [error, setError] = React.useState<string | null>(null)

  const selectedTemplate = templates.find((t) => t.id === templateId)
  // se o template lista GPUs compatíveis, restringe a seleção a elas
  const availableGpus =
    selectedTemplate && selectedTemplate.gpu_types.length > 0
      ? gpus.filter((g) => selectedTemplate.gpu_types.includes(g.id))
      : gpus

  const selectedGpu = availableGpus.find((g) => g.id === gpuTypeId)
  // quantos usuários a GPU escolhida comporta para este modelo
  const gpuCapacity =
    selectedTemplate && selectedGpu?.memoryInGb != null
      ? vramSlots({
          vramGb: selectedGpu.memoryInGb,
          modelFootprintGb: selectedTemplate.model_footprint_gb,
          kvReserveGbPerUser: selectedTemplate.kv_reserve_gb_per_user,
        })
      : null
  const maxUsersNum = maxUsers.trim() ? Number(maxUsers) : null
  const overCapacity =
    gpuCapacity !== null && maxUsersNum !== null && maxUsersNum > gpuCapacity

  function onTemplateChange(id: string) {
    setError(null)
    setTemplateId(id)
    setGpuTypeId("")
    const tpl = templates.find((t) => t.id === id)
    setMaxUsers(tpl?.max_users != null ? String(tpl.max_users) : "")
  }

  function onSubmit(formData: FormData) {
    setError(null)
    startTransition(async () => {
      try {
        // redirect acontece na action; erros esperados voltam como { error }
        const result = await createMachine(formData)
        if (result?.error) setError(result.error)
      } catch (e) {
        // redirect() lança um erro interno do Next — não tratar como falha
        if (e && typeof e === "object" && "digest" in e) throw e
        setError(e instanceof Error ? e.message : "Erro ao criar máquina")
      }
    })
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setError(null)
      }}
    >
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
              onChange={(e) => onTemplateChange(e.target.value)}
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
              value={gpuTypeId}
              onChange={(e) => {
                setError(null)
                setGpuTypeId(e.target.value)
              }}
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
          <div className="flex flex-col gap-2">
            <Label htmlFor="max_users">Limite de usuários</Label>
            <Input
              id="max_users"
              name="max_users"
              type="number"
              min={1}
              step={1}
              placeholder="automático (VRAM)"
              value={maxUsers}
              onChange={(e) => setMaxUsers(e.target.value)}
            />
            {overCapacity ? (
              <p className="text-xs text-destructive">
                Esta GPU comporta no máximo {gpuCapacity} usuário(s) para este
                modelo.
              </p>
            ) : gpuCapacity !== null ? (
              <p className="text-xs text-muted-foreground">
                Esta GPU comporta até {gpuCapacity} usuário(s) para este modelo.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Vazio = limite calculado pela VRAM da GPU.
              </p>
            )}
          </div>
          {error && (
            <Alert variant="destructive">
              <AlertCircle />
              <AlertTitle>Não foi possível criar a máquina</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button
            type="submit"
            disabled={pending || templates.length === 0 || overCapacity}
          >
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
