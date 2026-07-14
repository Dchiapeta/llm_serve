"use client"

import * as React from "react"
import { Download } from "lucide-react"
import { toast } from "sonner"

import { importTemplate } from "@/lib/actions"
import type { GpuType } from "@/lib/runpod"
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
import { TEMPLATE_PLANS } from "@/lib/types"

export function ImportTemplateDialog({
  runpodTemplateId,
  name,
  image,
  gpus,
}: {
  runpodTemplateId: string
  name: string
  image: string
  gpus: GpuType[]
}) {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await importTemplate(formData)
        toast.success("Produto importado")
        setOpen(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao importar produto")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Download /> Importar
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Importar “{name}”</DialogTitle>
          <DialogDescription>
            Complete os parâmetros de modelo e capacidade. Imagem e disco vêm do
            RunPod.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
          <input type="hidden" name="runpod_template_id" value={runpodTemplateId} />

          <div className="flex flex-col gap-2">
            <Label>Imagem Docker (do RunPod)</Label>
            <Input value={image} readOnly className="font-mono text-xs" />
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor={`plan-${runpodTemplateId}`}>Plano</Label>
            <Select name="plan" defaultValue="VibeCoder">
              <SelectTrigger id={`plan-${runpodTemplateId}`} className="w-full">
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
            <Label htmlFor={`model_name-${runpodTemplateId}`}>Modelo</Label>
            <Input
              id={`model_name-${runpodTemplateId}`}
              name="model_name"
              placeholder="Qwen/Qwen2.5-7B-Instruct"
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor={`footprint-${runpodTemplateId}`}>Modelo (GB VRAM)</Label>
              <Input
                id={`footprint-${runpodTemplateId}`}
                name="model_footprint_gb"
                type="number"
                step="0.5"
                defaultValue={16}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor={`kv-${runpodTemplateId}`}>Reserva/usuário (GB)</Label>
              <Input
                id={`kv-${runpodTemplateId}`}
                name="kv_reserve_gb_per_user"
                type="number"
                step="0.5"
                defaultValue={2}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor={`lora-${runpodTemplateId}`}>Adapter LoRA (GB)</Label>
              <Input
                id={`lora-${runpodTemplateId}`}
                name="lora_footprint_gb"
                type="number"
                step="0.1"
                defaultValue={0.5}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor={`max-users-${runpodTemplateId}`}>Máx. usuários</Label>
              <Input
                id={`max-users-${runpodTemplateId}`}
                name="max_users"
                type="number"
                min={1}
                step={1}
                placeholder="automático (VRAM)"
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor={`gpu-count-${runpodTemplateId}`}>Quantidade de GPUs</Label>
              <Input
                id={`gpu-count-${runpodTemplateId}`}
                name="gpu_count"
                type="number"
                min={1}
                step={1}
                defaultValue={1}
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
          </div>

          <Button type="submit" disabled={pending}>
            {pending ? "Importando…" : "Importar produto"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
