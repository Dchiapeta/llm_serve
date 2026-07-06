"use client"

import * as React from "react"
import { toast } from "sonner"

import { updateTemplate } from "@/lib/actions"
import type { GpuType } from "@/lib/runpod"
import type { Template } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"

export function EditTemplateDialog({
  template,
  gpus,
  open,
  onOpenChange,
}: {
  template: Template
  gpus: GpuType[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [pending, startTransition] = React.useTransition()
  const selectedGpus = new Set(template.gpu_types ?? [])

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await updateTemplate(formData)
        toast.success("Template atualizado")
        onOpenChange(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao atualizar template")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] grid-rows-[auto_1fr] overflow-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Editar template</DialogTitle>
          <DialogDescription>
            Ajuste a imagem Docker, o modelo e os parâmetros de capacidade.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex min-h-0 flex-col gap-4 overflow-y-auto pr-1">
          <input type="hidden" name="id" value={template.id} />
          <div className="flex flex-col gap-2">
            <Label htmlFor="edit-name">Nome</Label>
            <Input
              id="edit-name"
              name="name"
              placeholder="vllm-qwen-7b"
              defaultValue={template.name}
              required
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="edit-model_name">Modelo (Hugging Face)</Label>
            <Input
              id="edit-model_name"
              name="model_name"
              placeholder="Qwen/Qwen2.5-7B-Instruct"
              pattern="[^/\s]+/[^/\s]+"
              title="Use o ID do repositório do Hugging Face no formato org/modelo"
              defaultValue={template.model_name}
              required
            />
            <p className="text-xs text-muted-foreground">
              ID do repositório no Hugging Face (org/modelo). Copie de
              huggingface.co e cole aqui — ex.: Qwen/Qwen2.5-7B-Instruct
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="edit-image">Imagem Docker</Label>
            <Input
              id="edit-image"
              name="image"
              placeholder="seuusuario/vllm-agent:latest"
              defaultValue={template.image}
              required
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="edit-start_command">Container start command</Label>
            <Textarea
              id="edit-start_command"
              name="start_command"
              rows={6}
              placeholder={"--model org/modelo\n--served-model-name meu-modelo\n--port 8000"}
              defaultValue={template.start_command ?? ""}
              className="font-mono text-xs"
            />
            <p className="text-xs text-muted-foreground">
              Argumentos passados ao container na inicialização. Um por linha ou
              separados por espaço.
            </p>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-disk_gb">Disco (GB)</Label>
              <Input
                id="edit-disk_gb"
                name="disk_gb"
                type="number"
                defaultValue={template.disk_gb}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-model_footprint_gb">Modelo (GB VRAM)</Label>
              <Input
                id="edit-model_footprint_gb"
                name="model_footprint_gb"
                type="number"
                step="0.5"
                defaultValue={template.model_footprint_gb}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-kv_reserve_gb_per_user">Reserva/usuário (GB)</Label>
              <Input
                id="edit-kv_reserve_gb_per_user"
                name="kv_reserve_gb_per_user"
                type="number"
                step="0.5"
                defaultValue={template.kv_reserve_gb_per_user}
              />
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label>GPUs compatíveis</Label>
            {gpus.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                Nenhuma GPU disponível (falha ao consultar o RunPod).
              </p>
            ) : (
              <div className="max-h-48 overflow-y-auto rounded-md border p-2">
                {gpus.map((g) => (
                  <label
                    key={g.id}
                    className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted"
                  >
                    <input
                      type="checkbox"
                      name="gpu_types"
                      value={g.id}
                      defaultChecked={selectedGpus.has(g.id)}
                      className="size-4 accent-primary"
                    />
                    <span className="flex-1">{g.displayName}</span>
                    <span className="text-xs text-muted-foreground">
                      {g.memoryInGb} GB
                      {g.securePrice ? ` · $${g.securePrice.toFixed(2)}/h` : ""}
                    </span>
                  </label>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              Selecione uma ou mais GPUs em que este modelo pode rodar.
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="edit-env">Variáveis de ambiente (JSON)</Label>
            <Textarea
              id="edit-env"
              name="env"
              rows={3}
              defaultValue={JSON.stringify(template.env ?? {}, null, 2)}
              className="font-mono text-xs"
            />
          </div>
          <Button type="submit" disabled={pending}>
            {pending ? "Salvando…" : "Salvar alterações"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
