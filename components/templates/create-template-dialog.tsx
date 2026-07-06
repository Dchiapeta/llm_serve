"use client"

import * as React from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"

import { createTemplate } from "@/lib/actions"
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
import { Textarea } from "@/components/ui/textarea"

export function CreateTemplateDialog({ gpus }: { gpus: GpuType[] }) {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await createTemplate(formData)
        toast.success("Template criado")
        setOpen(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao criar template")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Novo template
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Novo template</DialogTitle>
          <DialogDescription>
            Define a imagem Docker, o modelo e os parâmetros de capacidade.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="name">Nome</Label>
            <Input id="name" name="name" placeholder="vllm-qwen-7b" required />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="model_name">Modelo (Hugging Face)</Label>
            <Input
              id="model_name"
              name="model_name"
              placeholder="Qwen/Qwen2.5-7B-Instruct"
              pattern="[^/\s]+/[^/\s]+"
              title="Use o ID do repositório do Hugging Face no formato org/modelo"
              required
            />
            <p className="text-xs text-muted-foreground">
              ID do repositório no Hugging Face (org/modelo). Copie de
              huggingface.co e cole aqui — ex.: Qwen/Qwen2.5-7B-Instruct
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="image">Imagem Docker</Label>
            <Input
              id="image"
              name="image"
              placeholder="seuusuario/vllm-agent:latest"
              required
            />
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="disk_gb">Disco (GB)</Label>
              <Input id="disk_gb" name="disk_gb" type="number" defaultValue={40} />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="model_footprint_gb">Modelo (GB VRAM)</Label>
              <Input
                id="model_footprint_gb"
                name="model_footprint_gb"
                type="number"
                step="0.5"
                defaultValue={16}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="kv_reserve_gb_per_user">Reserva/usuário (GB)</Label>
              <Input
                id="kv_reserve_gb_per_user"
                name="kv_reserve_gb_per_user"
                type="number"
                step="0.5"
                defaultValue={2}
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
            <p className="text-xs text-muted-foreground">
              Selecione uma ou mais GPUs em que este modelo pode rodar.
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="env">Variáveis de ambiente (JSON)</Label>
            <Textarea
              id="env"
              name="env"
              rows={3}
              defaultValue={`{}`}
              className="font-mono text-xs"
            />
          </div>
          <Button type="submit" disabled={pending}>
            {pending ? "Criando…" : "Criar template"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
